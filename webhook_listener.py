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
import os
from pathlib import Path

from load_env import load_layered
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import store

SCRIPT_DIR = Path(__file__).resolve().parent
load_layered()

SENDGRID_WEBHOOK_PUBLIC_KEY = os.getenv("SENDGRID_WEBHOOK_PUBLIC_KEY", "")
MAILGUN_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
# Inbound parse webhooks aren't signed by the provider; protect them with a
# shared secret in the query string (?key=...) that only you and the provider know.
INBOUND_SECRET = os.getenv("INBOUND_SECRET", "")
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


@app.get("/leads")
async def leads(key: str = ""):
    """Quick CSV export — gated by INBOUND_SECRET so it's not public."""
    if not INBOUND_SECRET or not hmac.compare_digest(key, INBOUND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        df = store._read_df()
        return JSONResponse(df.to_dict(orient="records"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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

    if not first_name or not email or "@" not in email:
        return JSONResponse({"error": "first_name and email required"}, status_code=400)

    added = store.add_lead(first_name, email, phone)
    return JSONResponse({"added": added, "duplicate": not added}, status_code=200)
