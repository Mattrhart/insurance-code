import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@example.com")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
CALENDLY_LINK = os.getenv("CALENDLY_LINK", "[Your Calendly Link]")

FILE_PATH = SCRIPT_DIR / "data" / "rookie_list.csv"
REQUIRED_COLUMNS = ("First Name", "Business Phone")
BATCH_SIZE = 100
STATES = ["Pending", "EmailSent", "Replied", "Qualified", "Booked", "Stopped", "DNC"]
SUPPRESSION_STATES = {"Stopped", "DNC"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_phone(raw_phone) -> str:
    if pd.isna(raw_phone):
        raise ValueError("Missing phone number")
    if isinstance(raw_phone, float):
        raw_phone = int(raw_phone)
    digits = "".join(c for c in str(raw_phone) if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise ValueError(f"Invalid phone number: {raw_phone}")


def normalize_status(raw_status: object) -> str:
    value = str(raw_status).strip() if raw_status is not None else ""
    if value in STATES:
        return value
    if value == "Completed":
        return "Booked"
    return "Pending"


def is_suppressed(phone: str, suppression_set: set[str]) -> bool:
    return bool(phone and phone in suppression_set)


def build_suppression_set(df: pd.DataFrame) -> set[str]:
    suppressed: set[str] = set()
    for _, row in df.iterrows():
        if row["Status"] in SUPPRESSION_STATES:
            try:
                suppressed.add(format_phone(row["Business Phone"]))
            except ValueError:
                # If phone cannot be normalized, we cannot safely apply suppression by number.
                continue
    return suppressed


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    if "Status" not in df.columns:
        df["Status"] = "Pending"
    df["Status"] = df["Status"].apply(normalize_status)

    optional_columns = (
        "EmailSentAt",
        "RepliedAt",
        "QualifiedAt",
        "BookedAt",
        "StoppedAt",
        "DNCAppliedAt",
        "LastActionAt",
        "LastActionNote",
    )
    for col in optional_columns:
        if col not in df.columns:
            df[col] = ""
    return df


def send_email(first_name: str, recipient_email: str) -> tuple[bool, str]:
    """
    Email sender skeleton.
    Replace this with SendGrid API if preferred, but keep the return contract.
    """
    if not recipient_email or pd.isna(recipient_email):
        return False, "missing email address"
    if not SMTP_HOST:
        return False, "SMTP_HOST not set in .env"

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = recipient_email
    msg["Subject"] = "Open architecture math for your agency path"
    msg.set_content(
        (
            f"Hey {first_name},\n\n"
            "Saw you got your life license recently. If you are open to comparing "
            "an open-architecture model against a captive split, I can share the numbers.\n\n"
            f"You can also grab time here: {CALENDLY_LINK}\n"
        )
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True, "email sent"
    except Exception as exc:
        return False, f"email send failed: {exc}"


def attempt_qualification(row: pd.Series) -> tuple[str, str]:
    """
    Qualification skeleton:
    - Use webhook payload data, CRM attributes, or NLP signals to make decisions.
    """
    qualification_signal = str(row.get("QualificationSignal", "")).strip().lower()
    if qualification_signal in {"yes", "qualified", "hot"}:
        return "Qualified", "qualification signal matched"
    return "Replied", "waiting for qualification signal"


def handle_webhook_event(df: pd.DataFrame, event: dict[str, str]) -> bool:
    """
    Skeleton webhook handler for external triggers.
    Example event:
      {"phone": "+15551234567", "type": "replied"}
      {"phone": "+15551234567", "type": "dnc"}
    """
    event_type = str(event.get("type", "")).strip().lower()
    phone = str(event.get("phone", "")).strip()
    if not phone:
        return False

    try:
        normalized_phone = format_phone(phone)
    except ValueError:
        return False

    matched = False
    for index, row in df.iterrows():
        try:
            row_phone = format_phone(row["Business Phone"])
        except ValueError:
            continue
        if row_phone != normalized_phone:
            continue

        now = utc_now_iso()
        if event_type == "replied":
            df.at[index, "Status"] = "Replied"
            df.at[index, "RepliedAt"] = now
            df.at[index, "LastActionNote"] = "webhook reply received"
        elif event_type == "qualified":
            df.at[index, "Status"] = "Qualified"
            df.at[index, "QualifiedAt"] = now
            df.at[index, "LastActionNote"] = "webhook qualified signal received"
        elif event_type == "booked":
            df.at[index, "Status"] = "Booked"
            df.at[index, "BookedAt"] = now
            df.at[index, "LastActionNote"] = "webhook booking confirmation received"
        elif event_type == "stopped":
            df.at[index, "Status"] = "Stopped"
            df.at[index, "StoppedAt"] = now
            df.at[index, "LastActionNote"] = "webhook stop request received"
        elif event_type == "dnc":
            df.at[index, "Status"] = "DNC"
            df.at[index, "DNCAppliedAt"] = now
            df.at[index, "LastActionNote"] = "webhook dnc request received"
        else:
            return False

        df.at[index, "LastActionAt"] = now
        matched = True
    return matched


def load_targets() -> pd.DataFrame:
    if not FILE_PATH.exists():
        raise SystemExit(f"Target database not found: {FILE_PATH}")

    try:
        df = pd.read_csv(FILE_PATH, dtype={"Business Phone": str})
    except Exception as e:
        raise SystemExit(f"Failed to read {FILE_PATH}: {e}") from e

    missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_columns:
        raise SystemExit(
            f"CSV is missing required columns: {', '.join(missing_columns)}"
        )

    return ensure_columns(df)


def save_targets(df: pd.DataFrame) -> None:
    try:
        df.to_csv(FILE_PATH, index=False)
    except Exception as e:
        print(f"Failed to save database to {FILE_PATH}: {e}")


def process_row(index: int, row: pd.Series, df: pd.DataFrame, suppression_set: set[str]) -> bool:
    first_name = row["First Name"]
    status = row["Status"]
    now = utc_now_iso()

    try:
        phone = format_phone(row["Business Phone"])
    except ValueError as exc:
        df.at[index, "Status"] = "Stopped"
        df.at[index, "StoppedAt"] = now
        df.at[index, "LastActionAt"] = now
        df.at[index, "LastActionNote"] = f"invalid phone: {exc}"
        print(f"Stopped {first_name}: invalid phone ({exc})")
        return True

    if is_suppressed(phone, suppression_set):
        if status not in SUPPRESSION_STATES:
            df.at[index, "Status"] = "Stopped"
            df.at[index, "StoppedAt"] = now
            df.at[index, "LastActionAt"] = now
            df.at[index, "LastActionNote"] = "suppression match applied"
            print(f"Suppressed {first_name} due to suppression list match.")
            return True
        return False

    if status == "Pending":
        success, note = send_email(first_name=first_name, recipient_email=row.get("Email Address", ""))
        df.at[index, "LastActionAt"] = now
        df.at[index, "LastActionNote"] = note
        if success:
            df.at[index, "Status"] = "EmailSent"
            df.at[index, "EmailSentAt"] = now
            print(f"Email sent to {first_name}; state moved to EmailSent.")
            return True
        print(f"Email send skipped/failed for {first_name}: {note}")
        return True

    if status == "EmailSent":
        # Non-blocking: wait for webhook to move this lead to Replied/Stopped/DNC.
        return False

    if status == "Replied":
        next_state, note = attempt_qualification(row)
        df.at[index, "Status"] = next_state
        if next_state == "Qualified":
            df.at[index, "QualifiedAt"] = now
        df.at[index, "LastActionAt"] = now
        df.at[index, "LastActionNote"] = note
        print(f"{first_name} moved from Replied to {next_state}.")
        return True

    if status == "Qualified":
        # Booking usually comes from scheduler/webhook. We do not block here.
        df.at[index, "LastActionAt"] = now
        df.at[index, "LastActionNote"] = "awaiting booking confirmation webhook"
        return True

    if status in {"Booked", "Stopped", "DNC"}:
        return False

    return False


def run_state_machine() -> None:
    print("Booting state machine...")
    df = load_targets()
    suppression_set = build_suppression_set(df)
    changes = 0

    actionable_states = {"Pending", "EmailSent", "Replied", "Qualified"}
    actionable_idx = df[df["Status"].isin(actionable_states)].head(BATCH_SIZE).index

    for index in actionable_idx:
        row = df.loc[index]
        if process_row(index=index, row=row, df=df, suppression_set=suppression_set):
            changes += 1

    if changes:
        save_targets(df)
        print(f"Cycle complete. {changes} row(s) updated and persisted to CSV.")
    else:
        print("Cycle complete. No state transitions required.")


def main() -> None:
    run_state_machine()


if __name__ == "__main__":
    main()
