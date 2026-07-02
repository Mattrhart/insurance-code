"""
store.py — the single source of truth, made safe for concurrent access.

Both email_sequencer.py and webhook_listener.py import THIS module so every
read/modify/write to rookie_list.csv goes through one place, behind one
cross-process file lock, with atomic replacement on disk.

Why a file lock and not threading.Lock:
    The sequencer (a cron job) and the listener (a long-running web service)
    are SEPARATE PROCESSES. A threading.Lock only coordinates threads inside a
    single process, so it would do nothing here. filelock coordinates across
    processes (and machines, if the lock sits on shared storage).

Why atomic write:
    We write to a temp file then os.replace() it over the real file. os.replace
    is atomic on the same filesystem, so a reader (or a crash) never sees a
    half-written CSV.

Scale note (read this before you ship to real volume):
    CSV is fine for a few thousand leads. Every update rewrites the whole file,
    and the lock serializes all writers. The moment you feel that hurt, swap the
    three functions below (_read_df / _atomic_write / the locked sections) for
    SQLite — same single-file simplicity, but real row-level atomicity and no
    full-file rewrites. The rest of the codebase won't have to change.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from load_env import load_layered
from filelock import FileLock, Timeout

SCRIPT_DIR = Path(__file__).resolve().parent
load_layered()

CSV_PATH = Path(os.getenv("LEADS_CSV", SCRIPT_DIR / "data" / "rookie_list.csv"))
LOCK_PATH = CSV_PATH.with_suffix(CSV_PATH.suffix + ".lock")
LOCK_TIMEOUT = int(os.getenv("LOCK_TIMEOUT_SECONDS", "30"))

# Used to sign unsubscribe links so recipients can't unsubscribe each other
# or enumerate your list. Set a long random value in .env.
APP_SECRET = os.getenv("APP_SECRET", "change-me-to-a-long-random-string")

# Canonical columns. Missing ones are created automatically on first write.
BASE_COLUMNS = [
    "First Name",
    "Email",
    "Business Phone",
    "Status",
    "EmailSentAt",
    "StatusUpdatedAt",
    "LastEvent",
    "LastEventAt",
]

# The state machine. Funnel rank is used so a late-arriving "delivered" event
# can never drag a lead that already "Replied" back down the funnel.
FUNNEL_RANK = {
    "Pending": 0,
    "EmailSent": 1,
    "Replied": 2,
    "LinkSent": 3,
    "Qualified": 4,
    "Booked": 5,
}
# Terminal suppression states always win, regardless of current funnel position.
SUPPRESSED = {"Stopped", "DNC"}

_lock = FileLock(str(LOCK_PATH), timeout=LOCK_TIMEOUT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_df() -> pd.DataFrame:
    if not CSV_PATH.exists():
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=BASE_COLUMNS).to_csv(CSV_PATH, index=False)
    df = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    for col in BASE_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df.loc[df["Status"] == "", "Status"] = "Pending"
    return df


def _atomic_write(df: pd.DataFrame) -> None:
    tmp = CSV_PATH.with_suffix(CSV_PATH.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, CSV_PATH)  # atomic on the same filesystem


# ----------------------------------------------------------------------------
# Unsubscribe token helpers (CAN-SPAM one-click unsubscribe, tamper-proof)
# ----------------------------------------------------------------------------
def make_unsub_token(email: str) -> str:
    return hmac.new(
        APP_SECRET.encode(), email.lower().encode(), hashlib.sha256
    ).hexdigest()


def verify_unsub_token(email: str, token: str) -> bool:
    return hmac.compare_digest(make_unsub_token(email), token or "")


def import_csv_bytes(data: bytes) -> tuple[int, int]:
    """
    Replace the ledger from an uploaded CSV. Accepts leads_segment.csv format
    (Email) or raw FL format (Email Address). Returns (rows_written, dupes_dropped).
    """
    import io

    df = pd.read_csv(io.BytesIO(data), dtype=str).fillna("")
    if "Email Address" in df.columns and "Email" not in df.columns:
        df["Email"] = df["Email Address"]
    if "First Name" not in df.columns or "Email" not in df.columns:
        raise ValueError("CSV must include 'First Name' and 'Email' columns")

    df["Email"] = df["Email"].str.lower().str.strip()
    df = df[df["Email"].str.contains("@", na=False)]
    if df.empty:
        raise ValueError("no valid email rows found")

    before = len(df)
    df = df.drop_duplicates(subset=["Email"], keep="first")
    dropped = before - len(df)

    for col in BASE_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df.loc[df["Status"].str.strip() == "", "Status"] = "Pending"

    with _lock:
        _atomic_write(df)
        return len(df), dropped


# ----------------------------------------------------------------------------
# Inbound opt-ins from the website form
# ----------------------------------------------------------------------------
def add_lead(first_name: str, email: str, phone: str = "") -> bool:
    """
    Add a new Pending lead if the email isn't already in the list.
    Returns True if added, False if it's a duplicate (already present).
    """
    with _lock:
        df = _read_df()
        if (df["Email"].str.lower() == email.lower().strip()).any():
            return False
        new_row = {col: "" for col in BASE_COLUMNS}
        new_row["First Name"] = first_name.strip().title()
        new_row["Email"] = email.lower().strip()
        new_row["Business Phone"] = phone.strip()
        new_row["Status"] = "Pending"
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        _atomic_write(df)
        return True


# ----------------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------------
def fetch_pending(limit: int, daily_cap: int) -> tuple[pd.DataFrame, int]:
    """
    Return up to `limit` Pending leads, capped so today's total sends never
    exceed `daily_cap`. Counting today's sends straight from the CSV means the
    cap holds even across multiple sequencer runs in the same day.
    """
    with _lock:
        df = _read_df()
        today = date.today().isoformat()
        sent_today = int(df["EmailSentAt"].str.startswith(today).sum())
        remaining = max(0, daily_cap - sent_today)
        n = min(limit, remaining)
        pending = (
            df[df["Status"] == "Pending"]
            .drop_duplicates(subset=["Email"], keep="first")
            .head(n)
            .copy()
        )
        return pending, sent_today


# ----------------------------------------------------------------------------
# Writes — all go through advance_status so the funnel logic stays in one place
# ----------------------------------------------------------------------------
def record_email_sent(email: str) -> bool:
    with _lock:
        df = _read_df()
        mask = df["Email"].str.lower() == email.lower()
        if not mask.any():
            return False
        now = _now()
        df.loc[mask, "Status"] = "EmailSent"
        df.loc[mask, "EmailSentAt"] = now
        df.loc[mask, "StatusUpdatedAt"] = now
        _atomic_write(df)
        return True


def get_lead(email: str) -> Optional[dict]:
    """Return the ledger row for an email address, or None if not found."""
    with _lock:
        df = _read_df()
        mask = df["Email"].str.lower() == email.lower().strip()
        if not mask.any():
            return None
        return df[mask].iloc[0].to_dict()


def record_link_sent(email: str) -> bool:
    return update_by_email(email, "LinkSent", "calendly_link_sent")


def _apply(
    df: pd.DataFrame,
    mask,
    new_status: str,
    event_name: str,
) -> bool:
    """Apply funnel/suppression rules to the masked rows. Returns True if changed."""
    changed = False
    now = _now()
    for idx in df[mask].index:
        current = df.at[idx, "Status"]
        # Record the raw event regardless of whether status moves. Touching the
        # event trail counts as a change so it always gets written to disk.
        df.at[idx, "LastEvent"] = event_name
        df.at[idx, "LastEventAt"] = now
        changed = True

        if new_status in SUPPRESSED:
            # Suppression always wins and is terminal.
            if current not in SUPPRESSED:
                df.at[idx, "Status"] = new_status
                df.at[idx, "StatusUpdatedAt"] = now
            changed = True
            continue

        if current in SUPPRESSED:
            # Never resurrect a suppressed lead from a delivery/click event.
            continue

        # Only ever advance forward through the funnel.
        if FUNNEL_RANK.get(new_status, -1) > FUNNEL_RANK.get(current, -1):
            df.at[idx, "Status"] = new_status
            df.at[idx, "StatusUpdatedAt"] = now
            changed = True
    return changed


def update_by_email(email: str, new_status: str, event_name: str) -> bool:
    with _lock:
        df = _read_df()
        mask = df["Email"].str.lower() == email.lower()
        if not mask.any():
            return False
        changed = _apply(df, mask, new_status, event_name)
        if changed:
            _atomic_write(df)
        return changed


def update_by_phone(phone_last10: str, new_status: str, event_name: str) -> bool:
    """Match Twilio SMS events on the last 10 digits to avoid +1 / formatting drift."""
    with _lock:
        df = _read_df()
        digits = df["Business Phone"].str.replace(r"\D", "", regex=True).str[-10:]
        mask = digits == phone_last10[-10:]
        if not mask.any():
            return False
        changed = _apply(df, mask, new_status, event_name)
        if changed:
            _atomic_write(df)
        return changed


# ----------------------------------------------------------------------------
# Inbound reply / SMS classification
# ----------------------------------------------------------------------------
STOP_WORDS = {"stop", "unsubscribe", "quit", "end", "cancel", "remove", "optout", "opt-out"}


def classify_inbound(body: str) -> str:
    """A reply is either an opt-out ('Stopped') or genuine engagement ('Replied')."""
    text = (body or "").strip().lower()
    first_token = text.split()[0] if text else ""
    if first_token in STOP_WORDS or text in STOP_WORDS:
        return "Stopped"
    return "Replied"
