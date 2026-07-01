"""
email_sender.py — plain-text outbound email via Resend HTTP API.

Shared by email_sequencer.py (cron) and webhook_listener.py (/send route).
Uses port 443 so Railway and other hosts that block SMTP can still send.
No HTML, no tracking pixels — plain text only for primary-inbox deliverability.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import requests

import store

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

FROM_EMAIL = os.environ.get("FROM_EMAIL")
FROM_NAME = os.environ.get("FROM_NAME", "")
REPLY_TO = os.environ.get("REPLY_TO", FROM_EMAIL)

COMPANY_NAME = os.environ.get("COMPANY_NAME", "")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
UNSUB_BASE_URL = os.environ.get(
    "UNSUB_BASE_URL", "https://your-listener.example.com/unsubscribe"
)
SUBJECT = os.environ.get("EMAIL_SUBJECT", "quick question about your book of business")
CALENDLY_LINK = os.environ.get("CALENDLY_LINK", "")
EMAIL2_SUBJECT = os.environ.get(
    "EMAIL2_SUBJECT", "calendar link — running the math"
)
RESEND_RECEIVING_URL = "https://api.resend.com/emails/receiving"


class SendError(Exception):
    """Resend send failed."""

    recipient_refused = False


class RecipientRefusedError(SendError):
    recipient_refused = True


def configured() -> bool:
    return bool(RESEND_API_KEY and FROM_EMAIL and COMPANY_ADDRESS)


def missing_config() -> list[str]:
    return [
        k
        for k, v in {
            "RESEND_API_KEY": RESEND_API_KEY,
            "FROM_EMAIL": FROM_EMAIL,
            "COMPANY_ADDRESS": COMPANY_ADDRESS,
        }.items()
        if not v
    ]


def _from_header() -> str:
    if FROM_NAME:
        return f"{FROM_NAME} <{FROM_EMAIL}>"
    return FROM_EMAIL


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
        "Are you open to seeing the backend of the pipeline?\n\n"
        "Reply 'yes' and I'll drop my calendar link to walk you through the math.\n\n"
        f"{FROM_NAME or COMPANY_NAME}\n"
        f"{COMPANY_NAME}\n"
        f"{COMPANY_ADDRESS}\n\n"
        "You're receiving this as a licensed-producer outreach. If you'd rather not hear "
        f"from me, unsubscribe here and I'll remove you immediately: {link}\n"
    )


def render_calendly_text(first_name: str) -> str:
    name = first_name.strip()
    greeting = f"Hi {name},\n\n" if name else ""
    signoff = FROM_NAME or "Matt"
    return (
        f"{greeting}"
        "Appreciate the reply.\n\n"
        f"Here is my direct calendar link so we can run the math: {CALENDLY_LINK}\n\n"
        f"Talk soon,\n{signoff}\n"
    )


def build_payload(first_name: str, to_email: str) -> dict:
    link = unsub_url(to_email)
    payload: dict = {
        "from": _from_header(),
        "to": [to_email],
        "subject": SUBJECT,
        "text": render_plain_text(first_name, to_email),
        "headers": {
            "List-Unsubscribe": f"<{link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    if REPLY_TO:
        payload["reply_to"] = REPLY_TO
    return payload


def send_one(first_name: str, to_email: str) -> str:
    """
    POST a plain-text email to Resend. Returns the Resend message id.
    Raises SendError or RecipientRefusedError on failure.
    """
    if not configured():
        raise SendError(f"missing config: {', '.join(missing_config())}")

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=build_payload(first_name, to_email),
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Resend request failed for %s: %s", to_email, exc)
        raise SendError(f"network_error: {exc}") from exc

    if resp.status_code == 200:
        return resp.json().get("id", "")

    detail = resp.text
    try:
        detail = resp.json().get("message", detail)
    except Exception:
        pass

    logger.error("Resend error for %s (%s): %s", to_email, resp.status_code, detail)

    if resp.status_code in {400, 422}:
        raise RecipientRefusedError(f"recipient_refused: {detail}")
    if resp.status_code in {401, 403}:
        raise SendError(f"auth_failed: {detail}")
    if resp.status_code == 429:
        raise SendError(f"rate_limited: {detail}")

    raise SendError(f"resend_error ({resp.status_code}): {detail}")


def fetch_received_text(email_id: str) -> str:
    """Fetch plain-text body of an inbound email from Resend (webhook is metadata-only)."""
    if not RESEND_API_KEY or not email_id:
        return ""
    try:
        resp = requests.get(
            f"{RESEND_RECEIVING_URL}/{email_id}",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            timeout=30,
        )
        if resp.status_code != 200:
            return ""
        return resp.json().get("text") or ""
    except requests.RequestException as exc:
        logger.warning("failed to fetch received email %s: %s", email_id, exc)
        return ""


def send_calendly_link(to_email: str, first_name: str = "") -> str:
    """Email 2 — Calendly link after a reply. Returns Resend message id on 200."""
    if not configured():
        raise SendError(f"missing config: {', '.join(missing_config())}")
    if not CALENDLY_LINK:
        raise SendError("missing config: CALENDLY_LINK")

    payload: dict = {
        "from": _from_header(),
        "to": [to_email],
        "subject": EMAIL2_SUBJECT,
        "text": render_calendly_text(first_name),
    }
    if REPLY_TO:
        payload["reply_to"] = REPLY_TO

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.error("Resend calendly send failed for %s: %s", to_email, exc)
        raise SendError(f"network_error: {exc}") from exc

    if resp.status_code == 200:
        return resp.json().get("id", "")

    detail = resp.text
    try:
        detail = resp.json().get("message", detail)
    except Exception:
        pass
    logger.error("Resend calendly error for %s (%s): %s", to_email, resp.status_code, detail)
    raise SendError(f"resend_error ({resp.status_code}): {detail}")


def send_one_safe(first_name: str, to_email: str) -> tuple[bool, str]:
    """Send a single plain-text email. Returns (success, note). Never raises."""
    try:
        msg_id = send_one(first_name, to_email)
        return True, msg_id or "sent"
    except RecipientRefusedError as exc:
        logger.warning("recipient refused %s: %s", to_email, exc)
        return False, "recipient_refused"
    except SendError as exc:
        logger.error("send failed for %s: %s", to_email, exc)
        return False, str(exc)
    except Exception as exc:
        logger.exception("unexpected send failure for %s", to_email)
        return False, f"unexpected: {exc}"
