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

import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib.parse import quote

from load_env import load_layered

import store

SCRIPT_DIR = Path(__file__).resolve().parent
load_layered()

# --- SMTP / sender identity ---
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() == "true"  # True for port 465

FROM_EMAIL = os.getenv("FROM_EMAIL")
FROM_NAME = os.getenv("FROM_NAME", "")
REPLY_TO = os.getenv("REPLY_TO", FROM_EMAIL)

# --- CAN-SPAM required footer content ---
COMPANY_NAME = os.getenv("COMPANY_NAME", "")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")  # real physical mailing address
UNSUB_BASE_URL = os.getenv("UNSUB_BASE_URL", "https://your-listener.example.com/unsubscribe")

# --- Pacing / limits ---
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "50"))
DAILY_CAP = int(os.getenv("DAILY_CAP", "200"))
EMAILS_PER_HOUR = int(os.getenv("EMAILS_PER_HOUR", "60"))
MIN_INTERVAL = 3600.0 / max(1, EMAILS_PER_HOUR)  # seconds between sends
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

SUBJECT = os.getenv("EMAIL_SUBJECT", "quick question about your book of business")


def require_config() -> None:
    missing = [
        k for k, v in {
            "SMTP_HOST": SMTP_HOST, "SMTP_USER": SMTP_USER,
            "SMTP_PASS": SMTP_PASS, "FROM_EMAIL": FROM_EMAIL,
            "COMPANY_ADDRESS": COMPANY_ADDRESS,
        }.items() if not v
    ]
    if missing:
        sys.exit(f"Missing required .env values: {', '.join(missing)}")
    if "change-me" in store.APP_SECRET:
        sys.exit("Set a real APP_SECRET in .env (used to sign unsubscribe links).")


def unsub_url(email: str) -> str:
    token = store.make_unsub_token(email)
    return f"{UNSUB_BASE_URL}?lead={quote(email)}&token={token}"


def render(first_name: str, email: str) -> tuple[str, str]:
    """Return (plain_text, html). Keep the pitch honest; honest converts and survives scrutiny."""
    name = first_name.strip() or "there"
    link = unsub_url(email)

    text = f"""Hi {name},

I came across your profile and saw you're licensed and actively writing.

I help producers compare what they're keeping under their current contract
against an open-architecture model (higher splits, real ownership of your book,
overrides if you choose to build a team). No pressure and nothing to buy — if
the numbers don't beat what you have, you'll know in 15 minutes.

Worth a quick look? Just reply to this email and I'll send a couple of times.

{FROM_NAME or COMPANY_NAME}
{COMPANY_NAME}
{COMPANY_ADDRESS}

You're receiving this as a licensed-producer outreach. If you'd rather not hear
from me, unsubscribe here and I'll remove you immediately: {link}
"""

    html = f"""\
<html><body style="font-family:Arial,sans-serif;font-size:15px;color:#222;line-height:1.5">
<p>Hi {name},</p>
<p>I came across your profile and saw you're licensed and actively writing.</p>
<p>I help producers compare what they're keeping under their current contract
against an open-architecture model &mdash; higher splits, real ownership of your
book, and overrides if you choose to build a team. No pressure and nothing to
buy: if the numbers don't beat what you have, you'll know in 15 minutes.</p>
<p>Worth a quick look? Just reply and I'll send a couple of times.</p>
<p style="margin-top:24px">{FROM_NAME or COMPANY_NAME}<br>{COMPANY_NAME}</p>
<hr style="border:none;border-top:1px solid #ddd;margin:20px 0">
<p style="font-size:12px;color:#888">
{COMPANY_NAME} &middot; {COMPANY_ADDRESS}<br>
You're receiving this as a licensed-producer outreach.
<a href="{link}">Unsubscribe</a> and I'll remove you immediately.
</p>
</body></html>
"""
    return text, html


def build_message(first_name: str, to_email: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL)) if FROM_NAME else FROM_EMAIL
    msg["To"] = to_email
    msg["Reply-To"] = REPLY_TO
    msg["Subject"] = SUBJECT
    msg["Message-ID"] = make_msgid()

    # One-click unsubscribe at the mail-client level (Gmail/Outlook show a button).
    link = unsub_url(to_email)
    msg["List-Unsubscribe"] = f"<{link}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    text, html = render(first_name, to_email)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def connect() -> smtplib.SMTP:
    if SMTP_USE_SSL:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        smtp.starttls(context=ssl.create_default_context())
    smtp.login(SMTP_USER, SMTP_PASS)
    return smtp


def send_one(smtp: smtplib.SMTP, first_name: str, to_email: str) -> None:
    msg = build_message(first_name, to_email)
    smtp.send_message(msg)


def main() -> None:
    require_config()

    pending, sent_today = store.fetch_pending(limit=MAX_PER_RUN, daily_cap=DAILY_CAP)
    if pending.empty:
        print(f"No sendable Pending leads (already sent {sent_today} today, cap {DAILY_CAP}).")
        return

    print(f"Sending {len(pending)} emails (sent {sent_today} earlier today). "
          f"Pacing ~{EMAILS_PER_HOUR}/hr.")

    smtp = connect()
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
                    send_one(smtp, first, email)
                    store.record_email_sent(email)
                    sent += 1
                    print(f"  sent -> {email}")
                    break
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                    print(f"  reconnecting (attempt {attempt}) for {email}...")
                    try:
                        smtp.quit()
                    except Exception:
                        pass
                    time.sleep(2 * attempt)  # backoff
                    smtp = connect()
                except smtplib.SMTPRecipientsRefused:
                    print(f"  refused (bad address) -> {email}; marking DNC")
                    store.update_by_email(email, "DNC", "recipient_refused")
                    break
                except Exception as e:
                    print(f"  error attempt {attempt} for {email}: {e}")
                    time.sleep(2 * attempt)
            else:
                print(f"  gave up on {email} after {MAX_RETRIES} attempts")

            time.sleep(MIN_INTERVAL)  # rate-limit pacing
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
    print(f"  END OF RUN AUDIT")
    print(f"{'='*44}")
    print(f"  Sent this run      : {sent}")
    print(f"  Sent today (total) : {sent_today + sent}")
    print(f"  Errors this run    : {len(pending) - sent}")
    print(f"  Pending in ledger  : {remaining}")
    print(f"  Non-pending total  : {sent_total}")
    print(f"{'='*44}\n")


if __name__ == "__main__":
    main()
