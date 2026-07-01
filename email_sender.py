"""
email_sender.py — plain-text outbound email via Gmail SMTP.

Shared by email_sequencer.py (cron) and webhook_listener.py (/send route).
No HTML, no tracking pixels — plain text only for primary-inbox deliverability.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from urllib.parse import quote

import store

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")

FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
FROM_NAME = os.environ.get("FROM_NAME", "")
REPLY_TO = os.environ.get("REPLY_TO", FROM_EMAIL)

COMPANY_NAME = os.environ.get("COMPANY_NAME", "")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
UNSUB_BASE_URL = os.environ.get(
    "UNSUB_BASE_URL", "https://your-listener.example.com/unsubscribe"
)
SUBJECT = os.environ.get("EMAIL_SUBJECT", "quick question about your book of business")


def smtp_configured() -> bool:
    return bool(SMTP_USER and SMTP_APP_PASSWORD and FROM_EMAIL and COMPANY_ADDRESS)


def missing_config() -> list[str]:
    return [
        k
        for k, v in {
            "SMTP_USER": SMTP_USER,
            "SMTP_APP_PASSWORD": SMTP_APP_PASSWORD,
            "FROM_EMAIL": FROM_EMAIL,
            "COMPANY_ADDRESS": COMPANY_ADDRESS,
        }.items()
        if not v
    ]


def unsub_url(email: str) -> str:
    token = store.make_unsub_token(email)
    return f"{UNSUB_BASE_URL}?lead={quote(email)}&token={token}"


def render_plain_text(first_name: str, email: str) -> str:
    name = first_name.strip() or "there"
    link = unsub_url(email)
    return (
        f"Hi {name},\n\n"
        "I came across your profile and saw you're licensed and actively writing.\n\n"
        "I help producers compare what they're keeping under their current contract "
        "against an open-architecture model (higher splits, real ownership of your book, "
        "overrides if you choose to build a team). No pressure and nothing to buy — if "
        "the numbers don't beat what you have, you'll know in 15 minutes.\n\n"
        "Worth a quick look? Just reply to this email and I'll send a couple of times.\n\n"
        f"{FROM_NAME or COMPANY_NAME}\n"
        f"{COMPANY_NAME}\n"
        f"{COMPANY_ADDRESS}\n\n"
        "You're receiving this as a licensed-producer outreach. If you'd rather not hear "
        f"from me, unsubscribe here and I'll remove you immediately: {link}\n"
    )


def build_message(first_name: str, to_email: str) -> MIMEText:
    msg = MIMEText(render_plain_text(first_name, to_email), "plain", "utf-8")
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL)) if FROM_NAME else FROM_EMAIL
    msg["To"] = to_email
    msg["Reply-To"] = REPLY_TO
    msg["Subject"] = SUBJECT
    msg["Message-ID"] = make_msgid()
    link = unsub_url(to_email)
    msg["List-Unsubscribe"] = f"<{link}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return msg


def connect() -> smtplib.SMTP_SSL:
    ctx = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30)
    smtp.login(SMTP_USER, SMTP_APP_PASSWORD)
    return smtp


def send_one(smtp: smtplib.SMTP_SSL, first_name: str, to_email: str) -> None:
    smtp.sendmail(FROM_EMAIL, [to_email], build_message(first_name, to_email).as_string())


def send_one_safe(first_name: str, to_email: str) -> tuple[bool, str]:
    """Send a single plain-text email. Returns (success, note). Never raises."""
    if not smtp_configured():
        return False, f"missing config: {', '.join(missing_config())}"
    try:
        with connect() as smtp:
            send_one(smtp, first_name, to_email)
        return True, "sent"
    except smtplib.SMTPRecipientsRefused as exc:
        logger.warning("recipient refused %s: %s", to_email, exc)
        return False, "recipient_refused"
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed: %s", exc)
        return False, "auth_failed"
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending to %s: %s", to_email, exc)
        return False, f"smtp_error: {exc}"
    except OSError as exc:
        logger.error("network error sending to %s: %s", to_email, exc)
        return False, f"network_error: {exc}"
    except Exception as exc:
        logger.exception("unexpected send failure for %s", to_email)
        return False, f"unexpected: {exc}"
