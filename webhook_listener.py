"""
webhook_listener.py — the Webhook Listener (FastAPI).

A long-running service. Run it with:
    uvicorn webhook_listener:app --host 0.0.0.0 --port 8000

It exposes endpoints for both kinds of provider webhook, because they are
DIFFERENT systems:

  EVENT webhooks  -> delivery/open/click/bounce/spam/unsubscribe (outbound status)
      /webhooks/sendgrid/events
      /webhooks/mailgun/events

  INBOUND parse   -> the recipient actually emailed you back (a real reply)
      /webhooks/sendgrid/inbound      (SendGrid "Inbound Parse")
      /webhooks/mailgun/inbound       (Mailgun "Routes")
      /inbound                        (Resend Inbound — auto-fires Email 2)

  SMS (warm only) -> for opted-in texting later
      /webhooks/twilio/sms

  Plus one-click unsubscribe landing:
      GET /unsubscribe?lead=<email>&token=<hmac>

Every endpoint verifies authenticity BEFORE touching the CSV. An unverified
request gets a 403 and is ignored. All state changes go through store.py, so the
funnel/suppression rules are enforced in exactly one place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from load_env import load_layered
from fastapi import FastAPI, File, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import email_sender
import store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
load_layered()

SENDGRID_WEBHOOK_PUBLIC_KEY = os.getenv("SENDGRID_WEBHOOK_PUBLIC_KEY", "")
MAILGUN_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# Inbound parse webhooks aren't signed by the provider; protect them with a
# shared secret in the query string (?key=...) that only you and the provider know.
INBOUND_SECRET = os.getenv("INBOUND_SECRET", "")
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # used for Twilio URL validation

app = FastAPI(title="Recruit Engine Webhook Listener")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ----------------------------------------------------------------------------
# Provider event -> state-machine status
# ----------------------------------------------------------------------------
# Outbound EVENT names map to funnel/suppression states. Anything terminal
# (bounce/dropped/spam/unsubscribe) becomes suppression; click is informational.
SENDGRID_EVENT_MAP = {
    "bounce": "DNC",
    "dropped": "DNC",
    "spamreport": "DNC",
    "unsubscribe": "DNC",
    "group_unsubscribe": "DNC",
    # "delivered", "open", "click" -> recorded as LastEvent, no status downgrade
}
MAILGUN_EVENT_MAP = {
    "failed": "DNC",          # permanent failures
    "complained": "DNC",      # spam complaint
    "unsubscribed": "DNC",
}


# ----------------------------------------------------------------------------
# SendGrid Event Webhook (signed with an Ed25519/ECDSA public key)
# ----------------------------------------------------------------------------
def verify_sendgrid(raw_body: bytes, signature: str, timestamp: str) -> bool:
    if not SENDGRID_WEBHOOK_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        from sendgrid.helpers.eventwebhook import EventWebhook
        ew = EventWebhook(SENDGRID_WEBHOOK_PUBLIC_KEY)
        key = ew.convert_public_key_to_ecdsa(SENDGRID_WEBHOOK_PUBLIC_KEY)
        return ew.verify_signature(raw_body.decode(), signature, timestamp, key)
    except Exception:
        return False


@app.post("/webhooks/sendgrid/events")
async def sendgrid_events(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
    ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
    if not verify_sendgrid(raw, sig, ts):
        return JSONResponse({"error": "bad signature"}, status_code=403)

    events = json.loads(raw.decode() or "[]")
    handled = 0
    for ev in events:
        email = ev.get("email")
        name = ev.get("event", "")
        if not email:
            continue
        new_status = SENDGRID_EVENT_MAP.get(name)
        if new_status:
            store.update_by_email(email, new_status, f"sendgrid:{name}")
        else:
            # delivered/open/click -> record event only, never downgrade funnel
            store.update_by_email(email, "_noop", f"sendgrid:{name}")
        handled += 1
    return {"handled": handled}


# ----------------------------------------------------------------------------
# Mailgun Event Webhook (HMAC-SHA256 over timestamp + token)
# ----------------------------------------------------------------------------
def verify_mailgun(timestamp: str, token: str, signature: str) -> bool:
    if not MAILGUN_SIGNING_KEY or not (timestamp and token and signature):
        return False
    digest = hmac.new(
        MAILGUN_SIGNING_KEY.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, digest)


@app.post("/webhooks/mailgun/events")
async def mailgun_events(request: Request):
    payload = await request.json()
    sig = payload.get("signature", {})
    if not verify_mailgun(sig.get("timestamp", ""), sig.get("token", ""), sig.get("signature", "")):
        return JSONResponse({"error": "bad signature"}, status_code=403)

    data = payload.get("event-data", {})
    name = data.get("event", "")
    email = (data.get("recipient") or "").strip()
    if not email:
        return {"handled": 0}

    new_status = MAILGUN_EVENT_MAP.get(name)
    if new_status:
        store.update_by_email(email, new_status, f"mailgun:{name}")
    else:
        store.update_by_email(email, "_noop", f"mailgun:{name}")
    return {"handled": 1}


# ----------------------------------------------------------------------------
# Inbound replies (the actual "Replied" / "Stopped" signal)
# These are unsigned by the provider, so we gate on a shared ?key= secret.
# ----------------------------------------------------------------------------
def inbound_authorized(request: Request) -> bool:
    if not INBOUND_SECRET:
        return False
    return hmac.compare_digest(request.query_params.get("key", ""), INBOUND_SECRET)


def _handle_reply(from_email: str, body: str) -> str:
    new_status = store.classify_inbound(body)  # 'Replied' or 'Stopped'
    store.update_by_email(from_email, new_status, "inbound_reply")
    return new_status


@app.post("/webhooks/sendgrid/inbound")
async def sendgrid_inbound(request: Request):
    if not inbound_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    form = await request.form()
    from_email = _extract_email(form.get("from", ""))
    body = form.get("text", "") or form.get("html", "")
    if not from_email:
        return {"handled": 0}
    return {"status": _handle_reply(from_email, body)}


@app.post("/webhooks/mailgun/inbound")
async def mailgun_inbound(request: Request):
    if not inbound_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    form = await request.form()
    from_email = _extract_email(form.get("sender", "") or form.get("from", ""))
    body = form.get("body-plain", "") or form.get("stripped-text", "")
    if not from_email:
        return {"handled": 0}
    return {"status": _handle_reply(from_email, body)}


def _extract_email(raw: str) -> str:
    """Pull bare address out of 'Name <addr@x.com>' style headers."""
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        return raw[raw.find("<") + 1 : raw.find(">")].strip().lower()
    return raw.lower()


# ----------------------------------------------------------------------------
# Resend Inbound Webhook — reply triggers Email 2 (Calendly link)
# ----------------------------------------------------------------------------
def verify_resend_webhook(payload: bytes, headers) -> bool:
    if not RESEND_WEBHOOK_SECRET:
        return False
    try:
        from svix.webhooks import Webhook

        wh = Webhook(RESEND_WEBHOOK_SECRET)
        wh.verify(
            payload.decode(),
            {
                "svix-id": headers.get("svix-id", ""),
                "svix-timestamp": headers.get("svix-timestamp", ""),
                "svix-signature": headers.get("svix-signature", ""),
            },
        )
        return True
    except Exception:
        return False


@app.post("/inbound")
async def resend_inbound(request: Request):
    """
    Resend fires this on email.received. Only processes replies from ledger
    leads in EmailSent state — ignores unknown senders and spam.
    """
    raw = await request.body()
    if not verify_resend_webhook(raw, request.headers):
        return JSONResponse({"error": "bad signature"}, status_code=403)

    try:
        event = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if event.get("type") != "email.received":
        return {"handled": 0, "reason": "ignored event type"}

    data = event.get("data", {})
    from_email = _extract_email(data.get("from", ""))
    email_id = data.get("email_id", "")
    if not from_email:
        return {"handled": 0, "reason": "missing sender"}

    lead = store.get_lead(from_email)
    if not lead:
        logger.info("inbound ignored — %s not in ledger", from_email)
        return {"handled": 0, "reason": "not in ledger"}

    status = (lead.get("Status") or "").strip()
    if status != "EmailSent":
        logger.info("inbound ignored — %s status is %s", from_email, status)
        return {"handled": 0, "reason": f"status is {status}"}

    body = email_sender.fetch_received_text(email_id)
    if store.classify_inbound(body) == "Stopped":
        store.update_by_email(from_email, "Stopped", "resend_inbound_optout")
        return {"handled": 1, "status": "Stopped"}

    store.update_by_email(from_email, "Replied", "resend_inbound_reply")

    first_name = (lead.get("First Name") or "").strip()
    try:
        msg_id = email_sender.send_calendly_link(from_email, first_name)
        store.record_link_sent(from_email)
        logger.info("calendly link sent -> %s (id: %s)", from_email, msg_id)
        return {"handled": 1, "status": "LinkSent", "msg_id": msg_id}
    except email_sender.SendError as exc:
        logger.error("calendly send failed for %s: %s", from_email, exc)
        return JSONResponse(
            {"handled": 1, "status": "Replied", "error": str(exc)},
            status_code=502,
        )
    except Exception as exc:
        logger.exception("inbound handler failed for %s", from_email)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ----------------------------------------------------------------------------
# Twilio inbound SMS (warm channel; HMAC-SHA1 over URL + sorted params)
# ----------------------------------------------------------------------------
@app.post("/webhooks/twilio/sms")
async def twilio_sms(request: Request):
    form = dict((await request.form()))
    signature = request.headers.get("X-Twilio-Signature", "")
    url = PUBLIC_BASE_URL.rstrip("/") + "/webhooks/twilio/sms"
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(url, form, signature):
            return PlainTextResponse("bad signature", status_code=403)
    except Exception:
        return PlainTextResponse("validation error", status_code=403)

    from_phone = form.get("From", "")
    body = form.get("Body", "")
    new_status = store.classify_inbound(body)
    store.update_by_phone(from_phone, new_status, "inbound_sms")
    # Empty TwiML keeps Twilio happy without auto-replying.
    return Response(content="<Response></Response>", media_type="application/xml")


# ----------------------------------------------------------------------------
# One-click unsubscribe (CAN-SPAM). Verifies the signed token, then DNCs.
# ----------------------------------------------------------------------------
@app.api_route("/unsubscribe", methods=["GET", "POST"])
async def unsubscribe(request: Request):
    email = request.query_params.get("lead", "")
    token = request.query_params.get("token", "")
    if not email or not store.verify_unsub_token(email, token):
        return HTMLResponse("<h3>Invalid or expired link.</h3>", status_code=400)
    store.update_by_email(email, "DNC", "unsubscribe_click")
    return HTMLResponse(
        "<h3>You're unsubscribed.</h3><p>You won't receive further emails. Sorry for the interruption.</p>"
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/send")
async def send_pending(key: str = ""):
    """
    Process a batch of Pending leads — send plain-text email via Resend API,
    mark EmailSent on success. Gated by INBOUND_SECRET for Railway cron hits.
    """
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    missing = email_sender.missing_config()
    if missing:
        return JSONResponse({"error": f"missing config: {', '.join(missing)}"}, status_code=503)

    max_per_run = int(os.getenv("MAX_PER_RUN", "15"))
    daily_cap = int(os.getenv("DAILY_CAP", "200"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    min_interval = 3600.0 / max(1, int(os.getenv("EMAILS_PER_HOUR", "60")))

    try:
        pending, sent_today = store.fetch_pending(limit=max_per_run, daily_cap=daily_cap)
    except Exception as exc:
        logger.exception("failed to read pending leads")
        return JSONResponse({"error": str(exc)}, status_code=500)

    if pending.empty:
        return {"sent": 0, "errors": 0, "message": "no pending leads", "sent_today": sent_today}

    sent = 0
    errors = 0

    try:
        pending_rows = list(pending.iterrows())
        for i, (_, row) in enumerate(pending_rows):
            email = (row["Email"] or "").strip()
            first = row["First Name"]
            if not email:
                continue

            delivered = False
            for attempt in range(1, max_retries + 1):
                try:
                    sender = email_sender.pick_from_email()
                    msg_id = email_sender.send_one(first, email, from_email=sender)
                    store.record_email_sent(email, sent_domain=sender)
                    sent += 1
                    delivered = True
                    logger.info("sent -> %s from %s (id: %s)", email, sender, msg_id)
                    break
                except email_sender.RecipientRefusedError:
                    logger.warning("recipient refused: %s", email)
                    store.update_by_email(email, "DNC", "recipient_refused")
                    errors += 1
                    delivered = True
                    break
                except email_sender.SendError as exc:
                    logger.error("send error for %s (attempt %s): %s", email, attempt, exc)
                    time.sleep(2 * attempt)
                except Exception as exc:
                    logger.exception("unexpected send error for %s", email)
                    time.sleep(2 * attempt)

            if not delivered:
                errors += 1

            if i < len(pending_rows) - 1:
                time.sleep(min_interval)
    except Exception as exc:
        logger.exception("send batch failed")
        return JSONResponse({"error": str(exc)}, status_code=500)

    return {
        "sent": sent,
        "errors": errors,
        "batch_size": len(pending),
        "sent_today": sent_today + sent,
    }


@app.get("/status")
async def status(key: str = ""):
    """Pipeline status summary — gated by INBOUND_SECRET."""
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        df = store._read_df()
        counts = df["Status"].value_counts().to_dict()
        emailed_rows = df[df["Status"] == "EmailSent"]
        return {
            "total": len(df),
            "unique_emails": df["Email"].nunique(),
            "by_status": counts,
            "pending": counts.get("Pending", 0),
            "emailed": counts.get("EmailSent", 0),
            "emailed_unique": emailed_rows["Email"].nunique(),
            "replied": counts.get("Replied", 0),
            "link_sent": counts.get("LinkSent", 0),
            "followups": counts.get("Replied", 0) + counts.get("LinkSent", 0),
            "warmup_domain_2": (
                store.count_sent_today_by_domain(email_sender.FROM_EMAIL_2)
                if email_sender.FROM_EMAIL_2
                else 0
            ),
            "warmup_cap_2": email_sender.WARMUP_DAILY_CAP_2 if email_sender.FROM_EMAIL_2 else 0,
            "qualified": counts.get("Qualified", 0),
            "booked": counts.get("Booked", 0),
            "dnc": counts.get("DNC", 0) + counts.get("Stopped", 0),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/followups")
async def followups(key: str = ""):
    """Leads who replied but haven't booked (Replied or LinkSent)."""
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        rows = store.fetch_followups()
        return {"count": len(rows), "leads": rows}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/leads")
async def leads(key: str = "", status: str = ""):
    """Ledger export — optional ?status=LinkSent filter. Gated by INBOUND_SECRET."""
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        if status.strip():
            return JSONResponse(store.fetch_by_status(status.strip()))
        df = store._read_df()
        return JSONResponse(df.to_dict(orient="records"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/import")
async def import_leads(key: str = "", file: UploadFile = File(...)):
    """
    Upload a CSV to replace the ledger — bypasses Railway volume UI.
    Use leads_segment.csv from your local machine.
    """
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        data = await file.read()
        count, dropped = store.import_csv_bytes(data)
        logger.info("imported %s leads (%s dupes dropped) to %s", count, dropped, store.CSV_PATH)
        return {
            "imported": count,
            "dupes_dropped": dropped,
            "path": str(store.CSV_PATH),
            "pending": count,
        }
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("import failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ----------------------------------------------------------------------------
# Seed / test send — force Email 1 to a specific address (bypasses Pending queue)
# ----------------------------------------------------------------------------
@app.post("/seed")
async def seed_send(
    key: str = "",
    email: str = "",
    first_name: str = "Matthew",
    domain: str = "contracting",
):
    """
    Send one Email 1 to a specific inbox for warmup / reply testing.
    domain=contracting (default) → FROM_EMAIL_2; domain=consulting → FROM_EMAIL.
    Gated by INBOUND_SECRET.
    """
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    email = (email or "").strip().lower()
    first_name = (first_name or "Matthew").strip() or "Matthew"
    if not email or "@" not in email:
        return JSONResponse({"error": "email required"}, status_code=400)

    missing = email_sender.missing_config()
    if missing:
        return JSONResponse({"error": f"missing config: {', '.join(missing)}"}, status_code=503)

    store.add_lead(first_name, email, "", source="seed")
    want = (domain or "contracting").strip().lower()
    if want in {"consulting", "primary", "1", "main"}:
        sender = email_sender.FROM_EMAIL
    else:
        sender = email_sender.FROM_EMAIL_2 or email_sender.FROM_EMAIL
    try:
        msg_id = email_sender.send_one(first_name, email, from_email=sender)
        store.record_email_sent(email, sent_domain=sender)
        logger.info("seed sent -> %s from %s (id: %s)", email, sender, msg_id)
        return {"sent": True, "email": email, "from": sender, "id": msg_id}
    except email_sender.RecipientRefusedError as exc:
        store.update_by_email(email, "DNC", "recipient_refused")
        return JSONResponse({"sent": False, "error": str(exc)}, status_code=422)
    except email_sender.SendError as exc:
        logger.error("seed send failed for %s: %s", email, exc)
        return JSONResponse({"sent": False, "error": str(exc)}, status_code=502)


# ----------------------------------------------------------------------------
# Inbound opt-in from the website lead-capture form
# ----------------------------------------------------------------------------
@app.post("/optin")
async def optin(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    first_name = (data.get("first_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    source = (data.get("source") or "").strip()

    if not first_name or not email or "@" not in email:
        return JSONResponse({"error": "first_name and email required"}, status_code=400)

    added = store.add_lead(first_name, email, phone, source=source)
    return JSONResponse({"added": added, "duplicate": not added}, status_code=200)
