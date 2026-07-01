"""
email_sequencer.py — the Cold Layer.

Pulls 'Pending' leads, sends one personalized cold email each, marks them
'EmailSent' with a timestamp. Paced to respect provider rate limits and a daily
cap, and CAN-SPAM compliant by construction (real From, physical address,
one-click unsubscribe).

Run it on a schedule, e.g. cron every 30 min during business hours:
    */30 9-17 * * 1-5  cd /path/to/recruit_engine && python email_sequencer.py

Compliance is not optional and it's baked in here, not bolted on:
  * From/Reply-To are your real, authenticated domain (no spoofing).
  * Every email carries a physical postal address (CAN-SPAM requires it).
  * Every email carries a working one-click unsubscribe -> webhook -> 'DNC',
    via a tamper-proof signed link AND the List-Unsubscribe header.
  * Subject lines must not be deceptive. Keep them honest.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sys
import time

from load_env import load_layered

import email_sender
import store

load_layered()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "50"))
DAILY_CAP = int(os.getenv("DAILY_CAP", "200"))
EMAILS_PER_HOUR = int(os.getenv("EMAILS_PER_HOUR", "60"))
MIN_INTERVAL = 3600.0 / max(1, EMAILS_PER_HOUR)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))


def require_config() -> None:
    missing = email_sender.missing_config()
    if missing:
        sys.exit(f"Missing required env vars: {', '.join(missing)}")
    if "change-me" in store.APP_SECRET:
        sys.exit("Set a real APP_SECRET in .env (used to sign unsubscribe links).")


def main() -> None:
    require_config()

    pending, sent_today = store.fetch_pending(limit=MAX_PER_RUN, daily_cap=DAILY_CAP)
    if pending.empty:
        print(f"No sendable Pending leads (already sent {sent_today} today, cap {DAILY_CAP}).")
        return

    print(
        f"Sending {len(pending)} emails via Gmail SMTP "
        f"(sent {sent_today} earlier today, pacing ~{EMAILS_PER_HOUR}/hr)."
    )

    smtp = email_sender.connect()
    sent = 0
    try:
        for _, row in pending.iterrows():
            email = (row["Email"] or "").strip()
            first = row["First Name"]
            if not email:
                print(f"  skip (no email): {first}")
                continue

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    email_sender.send_one(smtp, first, email)
                    store.record_email_sent(email)
                    sent += 1
                    print(f"  sent -> {email}")
                    break
                except smtplib.SMTPRecipientsRefused:
                    logger.warning("recipient refused: %s", email)
                    print(f"  refused (bad address) -> {email}; marking DNC")
                    store.update_by_email(email, "DNC", "recipient_refused")
                    break
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
                    logger.warning("SMTP disconnected for %s (attempt %s): %s", email, attempt, exc)
                    print(f"  reconnecting (attempt {attempt}) for {email}...")
                    try:
                        smtp.quit()
                    except Exception:
                        pass
                    time.sleep(2 * attempt)
                    smtp = email_sender.connect()
                except smtplib.SMTPException as exc:
                    logger.error("SMTP error for %s (attempt %s): %s", email, attempt, exc)
                    print(f"  smtp error attempt {attempt} for {email}: {exc}")
                    time.sleep(2 * attempt)
                except Exception as exc:
                    logger.exception("unexpected error for %s (attempt %s)", email, attempt)
                    print(f"  error attempt {attempt} for {email}: {exc}")
                    time.sleep(2 * attempt)
            else:
                print(f"  gave up on {email} after {MAX_RETRIES} attempts")

            time.sleep(MIN_INTERVAL)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    with store._lock:
        df = store._read_df()
    remaining = (df["Status"] == "Pending").sum()
    sent_total = (df["Status"] != "Pending").sum()

    print(f"\n{'='*44}")
    print("  END OF RUN AUDIT")
    print(f"{'='*44}")
    print(f"  Sent this run      : {sent}")
    print(f"  Sent today (total) : {sent_today + sent}")
    print(f"  Errors this run    : {len(pending) - sent}")
    print(f"  Pending in ledger  : {remaining}")
    print(f"  Non-pending total  : {sent_total}")
    print(f"{'='*44}\n")


if __name__ == "__main__":
    main()
