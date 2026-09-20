import json
import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

import booking
import conversation_store
import httpx
import llm_assistant
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from rag import load_index_if_needed

app = FastAPI(title="WhatsApp Webhook Gateway", version="1.0.0")


# ---------------------- P0 / reliability globals ----------------------------
# P0.5 Dedupe of duplicate Meta deliveries (at-least-once → exactly-once UX).
# Keep 10,000 message IDs in memory — enough for a week of traffic, tiny memory.
from collections import deque  # noqa: E402
_RECENT_WAMIDS: deque[str] = deque(maxlen=10_000)
_RECENT_WAMIDS_SET: set[str] = set()


def _seen_message_id(wamid: str) -> bool:
    """Add a message ID to the dedup window. Returns True if already seen."""
    if not wamid:
        return False
    if wamid in _RECENT_WAMIDS_SET:
        return True
    _RECENT_WAMIDS.append(wamid)
    _RECENT_WAMIDS_SET.add(wamid)
    # Bound the set to match the deque: drop the oldest if set grows.
    if len(_RECENT_WAMIDS_SET) > _RECENT_WAMIDS.maxlen * 1.2:  # type: ignore[operator]
        while len(_RECENT_WAMIDS_SET) > len(_RECENT_WAMIDS):
            # rebuild: faster than repeated discards at this size.
            break
        else:
            return False  # never hit because while-else; kept for clarity.
        _RECENT_WAMIDS_SET.clear()
        _RECENT_WAMIDS_SET.update(_RECENT_WAMIDS)
    return False


# P0.4 Instant "talk to a human" keyword escalation. No LLM latency.
# Matched EXACTLY on a stripped+lowercased inbound text. No partial match so we
# don't false-positive on "I'm a human trader at XYZ".
_HUMAN_ESCALATION_KEYWORDS: tuple[str, ...] = (
    "human",
    "person",
    "agent",
    "operator",
    "real person",
    "speak to someone",
    "talk to someone",
    "sales",
    "urgent",
    "escalate",
    "manager",
    "i want a person",
    "can i speak to",
)


def _looks_like_human_escalation(text: str) -> bool:
    t = text.strip().lower().rstrip("!?.")
    if not t:
        return False
    for kw in _HUMAN_ESCALATION_KEYWORDS:
        if t == kw:
            return True
        # Also match "human please", "urgent now", etc — trailing 1-word variations
        # without requiring an LLM round-trip.
        words = t.split()
        if len(words) <= 3 and any(w == kw for w in words for kw in _HUMAN_ESCALATION_KEYWORDS):
            return True
    return False


# P0.2 Replies for non-text inbound messages. Short, specific, brand-aligned,
# asks exactly 1 clarifying question so the buyer doesn't feel ignored.
_NONTEXT_MEDIA_REPLY: dict[str, str] = {
    "document": (
        "Thanks — I've received your document and forwarded it to the "
        "Petrobind trading desk for their review. They'll reply directly "
        "here with any comments. Quick clarifier: is this for a specific "
        "product grade (e.g. Bitumen 60/70) and destination port?"
    ),
    "image": (
        "Got the image and passed it to our trading desk. If this is a photo "
        "of a delivery/QC issue, just reply with the product, shipment date, "
        "and any notes and the team can action it. Otherwise: which grade or "
        "product is it related to?"
    ),
    "audio": (
        "Got your voice note — I've flagged it for the Petrobind trading desk "
        "and they'll listen + revert. To speed things up: is it regarding a "
        "quote request, logistics, or an existing shipment?"
    ),
    "voice": (
        "Got your voice note — flagged it for the trading desk. To speed "
        "things up: is it regarding a quote request, logistics, or an existing "
        "shipment?"
    ),
    "video": (
        "Got the video — forwarded to the trading desk. One quick thing: "
        "which product + destination port is this for, so the right person "
        "reviews it first?"
    ),
    "sticker": "",  # ignore — no reply needed (silent receipt)
    "reaction": "",  # ignore — no reply needed
    "contacts": (
        "Got the contact card — forwarded to our desk. Quick question: which "
        "Petrobind product and destination port should we associate with this "
        "contact?"
    ),
    "location": (
        "Got the location. If this is a destination port or a pickup point, "
        "let me know the target product + volume and I'll have the desk prep "
        "the next steps around it."
    ),
    "interactive": "",  # button reply etc → Meta will forward the button text; leave to the text branch
    "unknown": "",
}



@app.on_event("startup")
async def _startup_warm_index() -> None:
    # P0.6: Hard fail on startup if the Meta auth / phone ID env vars are missing.
    # We never want the service to boot up "successfully" while being unable to
    # send any WhatsApp replies — that leads to silent buyer-perceived outages.
    missing = []
    if not os.getenv("WHATSAPP_ACCESS_TOKEN"):
        missing.append("WHATSAPP_ACCESS_TOKEN")
    if not os.getenv("WHATSAPP_PHONE_NUMBER_ID"):
        missing.append("WHATSAPP_PHONE_NUMBER_ID")
    if not os.getenv("WHATSAPP_VERIFY_TOKEN"):
        missing.append("WHATSAPP_VERIFY_TOKEN (for GET /webhook handshake — may still accept POST webhook if subscribed already)")
    if missing:
        raise RuntimeError(
            f"FATAL: Render startup aborted — required WhatsApp env vars are missing: "
            f"{', '.join(missing)}. Check the Render Environment tab."
        )
    # Print a friendly status line so Render logs have a 1-line "what's alive"
    # summary without dumping secrets.
    def _mask(val: str) -> str:
        if not val:
            return "<unset>"
        if len(val) <= 10:
            return "<set>"
        return f"{val[:4]}…{val[-3:]}"
    openrouter_ok = bool(os.getenv("OPENROUTER_API_KEY"))
    smtp_ok = bool(os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD") and os.getenv("SALES_EMAIL"))
    print(
        "[STARTUP] WhatsApp tokens OK. "
        f"phone_number_id={_mask(PHONE_NUMBER_ID)!r} "
        f"OPENAI_API_KEY={'set' if os.getenv('OPENAI_API_KEY') else '<unset>'} "
        f"OPENROUTER_API_KEY={'set' if openrouter_ok else '<unset — LLM replies will use canned fallback>'} "
        f"SMTP={'configured' if smtp_ok else '<unconfigured — lead/handoff emails will be SKIPPED>'} "
        f"CAL_COM_BOOKING_LINK={'set' if booking.is_configured() else '<unset>'} "
        f"FOLLOWUPS_CRON_TOKEN={'set' if FOLLOWUPS_CRON_TOKEN else '<unset — /followups-scan will WARN each call>'}"
    )
    try:
        load_index_if_needed()
    except Exception as exc:  # pragma: no cover - best effort warmup
        print(f"[WARN] rag index warmup failed (continuing): {exc!r}")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _graph_url_for(phone_number_id: str) -> str:
    return (
        f"https://graph.facebook.com/{API_VERSION}/"
        f"{phone_number_id}/messages"
    )


GRAPH_URL = _graph_url_for(PHONE_NUMBER_ID) if PHONE_NUMBER_ID else ""

APP_NAME = "WhatsApp Webhook Gateway"
APP_COMPANY = "Milly"
APP_CONTACT_EMAIL = "mileyphua96@gmail.com"
APP_COUNTRY = "Singapore"


APP_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
    '<defs>'
    '<linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0%" stop-color="#25D366"/>'
    '<stop offset="100%" stop-color="#128C7E"/>'
    '</linearGradient>'
    '</defs>'
    '<rect width="512" height="512" rx="96" fill="url(#g)"/>'
    '<path fill="#ffffff" d="M408 128A196 196 0 0 0 128 412L104 456l48-20a196 196 0 1 0 256-308z"/>'
    '<circle cx="256" cy="256" r="20" fill="#128C7E"/>'
    '<path fill="none" stroke="#128C7E" stroke-width="16" stroke-linecap="round" '
    'd="M220 220c16-24 64-24 64 16 0 24-16 40-16 40s-8 8 0 16 40 16 56 0c24-24 16-80-24-96"/>'
    '</svg>'
)


PRIVACY_POLICY_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy - {APP_NAME}</title>
<meta name="robots" content="index, follow">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 40px 20px; background: #f7f8fa; color: #1f2937; line-height: 1.65; }}
  .wrap {{ max-width: 820px; margin: 0 auto; background: #fff; padding: 48px; border-radius: 16px;
          box-shadow: 0 2px 12px rgba(0,0,0,.06); }}
  h1 {{ font-size: 30px; margin: 0 0 8px; }}
  h2 {{ font-size: 20px; margin: 32px 0 12px; color: #0f766e; }}
  p, li {{ font-size: 15px; }}
  .last {{ color: #6b7280; font-size: 13px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>Privacy Policy</h1>
<p><strong>Effective date:</strong> 2026-09-20</p>

<p>This Privacy Policy describes how <strong>{APP_COMPANY}</strong> ("we", "us", "our")
collects, uses, and shares information in connection with the
<strong>{APP_NAME}</strong> application ("the App"), which provides customer support
and transactional communication services over the WhatsApp Business Platform.</p>

<h2>1. Information we collect</h2>
<p>We only collect the minimum information necessary to deliver the App's services:</p>
<ul>
  <li><strong>Messages:</strong> the contents of inbound and outbound WhatsApp messages
    (including timestamp, sender / recipient phone numbers in E.164 format, and message ID),
    only for the purpose of delivering customer support and transactional notifications.</li>
  <li><strong>Contact numbers:</strong> WhatsApp phone numbers provided by users when they
    opt-in by sending a message to our WhatsApp Business Phone Number or explicitly agreeing
    to receive updates through our website or order forms.</li>
  <li><strong>Delivery statuses:</strong> sent / delivered / read statuses for customer
    service quality monitoring.</li>
</ul>
<p>We do <strong>not</strong> collect names, email addresses, payment card data,
government identifiers, or other personal information unless a user voluntarily
provides it inside the body of a support message.</p>

<h2>2. How we use the information</h2>
<ul>
  <li>Reply to users' inbound messages within the 24-hour customer support window.</li>
  <li>Send transactional notifications (e.g., order confirmation, shipping updates)
    to users who have explicitly opted in.</li>
  <li>Operate, maintain, and troubleshoot the webhook gateway service.</li>
  <li>Comply with legal obligations, applicable laws, and WhatsApp's Business Messaging
    Policy.</li>
</ul>
<p>We do <strong>not</strong> use messages or phone numbers for advertising, marketing,
profiling, or selling to third parties.</p>

<h2>3. Legal basis for processing (GDPR)</h2>
<p>Processing is based on (a) the user's explicit opt-in consent for transactional
notifications, (b) performance of a contract for customer support services the user
has requested, or (c) our legitimate interest in delivering secure 2-way customer
communications, each balanced against user rights. Users may withdraw consent at
any time (see §7).</p>

<h2>4. Data sharing and transfers</h2>
<p>We share message data with the following sub-processors only to operate the service:</p>
<ul>
  <li><strong>Meta Platforms / WhatsApp Business Platform</strong> — message routing
    (see Meta Privacy Policy).</li>
  <li><strong>Cloud hosting providers</strong> (Render, Supabase, and equivalent) —
    infrastructure hosting, under contract with adequate data protection terms.</li>
</ul>
<p>No other third parties receive any personal data. For users in the EEA, UK, or
Switzerland, data transfers rely on Standard Contractual Clauses or equivalent
adequacy mechanisms.</p>

<h2>5. Retention &amp; deletion</h2>
<p>Message content and associated metadata are retained for <strong>7 calendar days</strong>
to support the 24-hour customer support window and resolve delivery disputes,
after which they are permanently and securely deleted. Opt-in phone numbers are
retained until the user opts out or requests deletion, and are deleted within
<strong>24 hours</strong> of such a request.</p>

<h2>6. Security</h2>
<p>We use industry-standard security practices including HTTPS/TLS 1.3 for all
endpoints, encrypted-at-rest storage, environment-based secret management, and
least-privilege access controls.</p>

<h2>7. User rights &amp; opt-out</h2>
<p>Users may at any time:</p>
<ul>
  <li>Stop receiving messages by replying <strong>STOP</strong> to our WhatsApp
    Business Phone Number.</li>
  <li>Request access to, correction of, or deletion of their personal data by
    emailing <a href="mailto:{APP_CONTACT_EMAIL}">{APP_CONTACT_EMAIL}</a>.</li>
  <li>Withdraw previously-given consent at any time, without affecting the
    lawfulness of processing carried out prior to withdrawal.</li>
</ul>
<p>We will respond to verified user requests within <strong>30 days</strong>.</p>

<h2>8. Children</h2>
<p>The App is not directed to children under the age of 13 (or 16 in the EEA).
We do not knowingly collect personal data from children.</p>

<h2>9. Changes to this policy</h2>
<p>We may update this Privacy Policy from time to time. Material changes will be
notified by posting the updated policy at this URL with a revised effective date.</p>

<h2>10. Contact</h2>
<p>Questions or requests — <a href="mailto:{APP_CONTACT_EMAIL}">{APP_CONTACT_EMAIL}</a>
<br>{APP_COMPANY}, {APP_COUNTRY}.</p>

<p class="last">Document ID: pp-{APP_NAME.lower().replace(' ', '-')} · v1.0</p>
</div>
</body>
</html>
"""


TERMS_OF_SERVICE_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terms of Service - {APP_NAME}</title>
<meta name="robots" content="index, follow">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 40px 20px; background: #f7f8fa; color: #1f2937; line-height: 1.65; }}
  .wrap {{ max-width: 820px; margin: 0 auto; background: #fff; padding: 48px; border-radius: 16px;
          box-shadow: 0 2px 12px rgba(0,0,0,.06); }}
  h1 {{ font-size: 30px; margin: 0 0 8px; }}
  h2 {{ font-size: 20px; margin: 32px 0 12px; color: #0f766e; }}
  p, li {{ font-size: 15px; }}
  .last {{ color: #6b7280; font-size: 13px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>Terms of Service</h1>
<p><strong>Effective date:</strong> 2026-09-20</p>

<p>These Terms of Service ("Terms") govern access to and use of
<strong>{APP_NAME}</strong> (the "Service") operated by <strong>{APP_COMPANY}</strong>
("we", "us", "our"). By sending a message to our WhatsApp Business Phone Number,
or otherwise using the Service, you agree to these Terms.</p>

<h2>1. Service description</h2>
<p>The Service enables customer support, order notifications, and transactional
communication over the WhatsApp Business Platform. It is provided on an
"as-is" and "as-available" basis for end-users who message or opt in.</p>

<h2>2. Eligibility &amp; opt-in</h2>
<ul>
  <li>You must be at least the age of majority in your jurisdiction to use the Service.</li>
  <li>Messages are sent only to users who (a) initiate a conversation by messaging
    our WhatsApp Business Phone Number, or (b) explicitly opt-in to transactional
    notifications through our website or order form.</li>
</ul>

<h2>3. Acceptable use</h2>
<p>You agree not to use the Service to send, receive, or facilitate:</p>
<ul>
  <li>Unsolicited promotional or spam content.</li>
  <li>Content that is unlawful, defamatory, abusive, harassing, fraudulent, or
    that infringes on third-party rights.</li>
  <li>Content that violates the WhatsApp Business Messaging Policy, Community
    Standards, or Commerce Policy as published by Meta Platforms, Inc.</li>
</ul>
<p>We reserve the right to suspend or block any user or number that violates
these Terms or applicable policies.</p>

<h2>4. Opt-out</h2>
<p>You may stop receiving messages at any time by replying <strong>STOP</strong>
to our WhatsApp Business Phone Number. Opt-out requests are honoured within
24 hours.</p>

<h2>5. Intellectual property</h2>
<p>All content, trademarks, logos, and software made available through the
Service are owned by {APP_COMPANY} or its licensors. Nothing in these Terms
grants any licence to use our trademarks or branding except as required to
display Service-provided content.</p>

<h2>6. Disclaimers</h2>
<p>To the maximum extent permitted by law:</p>
<ul>
  <li>We disclaim all warranties, express or implied, including merchantability,
    fitness for a purpose, and non-infringement.</li>
  <li>We do not warrant the Service will be uninterrupted or error-free, or
    that messages will be delivered by third-party carriers within any timeframe.</li>
</ul>

<h2>7. Limitation of liability</h2>
<p>Our total liability under these Terms, whether in contract, tort, or otherwise,
is limited to the greater of US$100 or the amount paid (if any) by the user for
the Service in the 12 months preceding the claim. We are not liable for any
indirect, incidental, special, or consequential damages.</p>

<h2>8. Governing law &amp; jurisdiction</h2>
<p>These Terms are governed by the laws of <strong>{APP_COUNTRY}</strong>,
without regard to its conflict of law rules. Disputes will be resolved exclusively
in the courts located in {APP_COUNTRY}.</p>

<h2>9. Contact</h2>
<p>Questions — <a href="mailto:{APP_CONTACT_EMAIL}">{APP_CONTACT_EMAIL}</a>
<br>{APP_COMPANY}, {APP_COUNTRY}.</p>

<p class="last">Document ID: tos-{APP_NAME.lower().replace(' ', '-')} · v1.0</p>
</div>
</body>
</html>
"""


from fastapi.responses import FileResponse, HTMLResponse, Response


_ASSETS_DIR = os.path.dirname(os.path.abspath(__file__))


@app.get("/privacy-policy")
async def privacy_policy() -> HTMLResponse:
    return HTMLResponse(content=PRIVACY_POLICY_HTML, status_code=200)


@app.get("/terms-of-service")
async def terms_of_service() -> HTMLResponse:
    return HTMLResponse(content=TERMS_OF_SERVICE_HTML, status_code=200)


@app.get("/app-icon.svg")
async def app_icon_svg() -> Response:
    return Response(
        content=APP_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Content-Disposition": 'inline; filename="app-icon.svg"'},
    )


@app.get("/app-icon.png")
async def app_icon_png() -> FileResponse:
    return FileResponse(
        path=os.path.join(_ASSETS_DIR, "app-icon.png"),
        media_type="image/png",
        filename="app-icon.png",
    )


@app.get("/")
async def root() -> JSONResponse:
    debug_token = VERIFY_TOKEN if not VERIFY_TOKEN else (
        VERIFY_TOKEN[:6] + "…" + VERIFY_TOKEN[-4:]
    )
    return JSONResponse(
        content={
            "app": APP_NAME,
            "version": "1.0.0",
            "verify_token_loaded": bool(VERIFY_TOKEN),
            "verify_token_preview": debug_token,
            "phone_number_id_loaded": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
            "access_token_loaded": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")),
            "privacy_policy_url": "/privacy-policy",
            "terms_of_service_url": "/terms-of-service",
            "app_icon_url": "/app-icon.svg",
        }
    )



@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(default=None, alias="hub.mode"),
    hub_verify_token: str = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str = Query(default=None, alias="hub.challenge"),
) -> Any:
    if not VERIFY_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Verify token is not configured",
        )

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(
            content=hub_challenge, status_code=status.HTTP_200_OK
        )

    if (
        hub_mode is None
        and hub_verify_token is None
        and hub_challenge is None
    ):
        return JSONResponse(
            content={"status": "ok", "endpoint": "webhook"},
            status_code=status.HTTP_200_OK,
        )

    debug_expected = (
        VERIFY_TOKEN[:6] + "…" + VERIFY_TOKEN[-4:]
        if len(VERIFY_TOKEN) > 12
        else "****"
    )
    debug_got = (
        hub_verify_token[:6] + "…" + hub_verify_token[-4:]
        if hub_verify_token and len(hub_verify_token) > 12
        else (hub_verify_token or "None")
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "reason": "Verification failed",
            "mode_matched": hub_mode == "subscribe",
            "token_expected_preview": debug_expected,
            "token_got_preview": debug_got,
            "challenge_provided": bool(hub_challenge),
        },
    )


@app.post("/webhook")
async def receive_webhook(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    raw = await request.body()

    payload: Dict[str, Any] = {}
    if "application/json" in content_type.lower() or (
        raw and raw[:1] in (b"{", b"[")
    ):
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            if form:
                payload = {k: v for k, v in form.items()}
                if "entry" in payload:
                    payload["entry"] = [{"changes": []}]
        except Exception:
            payload = {}

    obj = payload.get("object")
    if obj and obj != "whatsapp_business_account":
        print(f"[WARN] unexpected object={obj!r} — still acking 200")

    try:
        for entry in payload.get("entry", []) or []:
            for change in (entry or {}).get("changes", []) or []:
                field = (change or {}).get("field")
                value = (change or {}).get("value", {}) or {}
                print(f"[WEBHOOK] field={field!r} value.keys={list(value.keys())}")
                metadata = value.get("metadata", {}) or {}
                pnid = metadata.get("phone_number_id") or PHONE_NUMBER_ID
                for message in value.get("messages", []) or []:
                    wamid = message.get("id")
                    # P0.5 Dedupe — Meta does AT-LEAST-ONCE delivery; skip known IDs silently.
                    if _seen_message_id(wamid):
                        print(f"[SKIP DUP] id={wamid!r} already processed recently")
                        continue

                    msg_type = message.get("type")
                    text_body = None
                    if msg_type == "text":
                        text_body = (message.get("text") or {}).get("body")
                        # P0.4 Instant "human" keyword escalation — deterministic, before LLM.
                        if isinstance(text_body, str) and _looks_like_human_escalation(text_body):
                            await _instant_handoff_reply(
                                from_number=message.get("from"),
                                phone_number_id=pnid,
                                reply_to_message_id=wamid,
                                reason=f"Instant escalation keyword: {text_body!r}",
                            )
                            _process_message(message)
                            continue

                        _process_message(message)
                        await _llm_reply(
                            from_number=message.get("from"),
                            phone_number_id=pnid,
                            inbound_text=text_body,
                            reply_to_message_id=wamid,
                        )
                        continue

                    # P0.2 Non-text inbound media messages.
                    _process_message(message)
                    canned = _NONTEXT_MEDIA_REPLY.get(msg_type)
                    summary_extra = ""
                    if canned:
                        try:
                            res = await send_whatsapp_text(
                                to=message.get("from"),
                                text=canned,
                                phone_number_id=pnid,
                                reply_to_message_id=wamid,
                                preview_url=False,
                            )
                            sent_id = (res.get("messages") or [{}])[0].get("id")
                            print(
                                f"[MEDIA ACK OK] type={msg_type!r} from={message.get('from')!r} "
                                f"sent_id={sent_id}"
                            )
                            summary_extra = (
                                f"Buyer sent a WhatsApp {msg_type} message. We auto-replied "
                                f"asking for product+port. Buyer original message_id={wamid}"
                            )
                        except Exception as exc:
                            print(f"[MEDIA ACK FAIL] type={msg_type!r} error={exc!r}")
                            summary_extra = (
                                f"Buyer sent a WhatsApp {msg_type} message (msg_id={wamid}). "
                                f"AUTO-REPLY FAILED with {exc!r} — please reach out manually."
                            )
                        # Also send a handoff email so a human sees the inbound media now.
                        try:
                            await notify.send_handoff_email(
                                phone_number=message.get("from") or "",
                                reason=(
                                    f"Inbound {msg_type} message received (no auto-processing)."
                                ),
                                partial_inquiry_summary=summary_extra,
                            )
                        except Exception as exc:
                            print(f"[MEDIA HANDOFF EMAIL FAIL] {exc!r}")
                    else:
                        print(
                            f"[MEDIA SILENT] type={msg_type!r} from={message.get('from')!r} "
                            f"-> no reply (sticker / reaction / interactive button-text will arrive "
                            f"as a separate text message if needed)."
                        )
                for st in value.get("statuses", []) or []:
                    _process_status(st)
                for event in value.get("contacts", []) or []:
                    print(f"[CONTACTS] event={event!r}")
    except Exception as exc:  # pragma: no cover
        print(f"[ERROR] processing payload: {exc!r}")

    return JSONResponse(
        content={"status": "success", "message": "EVENT_RECEIVED"},
        status_code=status.HTTP_200_OK,
    )


@app.api_route(
    "/webhook",
    methods=["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def webhook_catchall(request: Request) -> JSONResponse:
    return JSONResponse(
        content={"status": "ok", "method": request.method},
        status_code=status.HTTP_200_OK,
    )


def _process_message(message: Dict[str, Any]) -> None:
    message_id = message.get("id")
    from_number = message.get("from")
    message_type = message.get("type")
    timestamp = message.get("timestamp")

    text_body = None
    if message_type == "text":
        text_body = message.get("text", {}).get("body")

    print(
        f"[MESSAGE] id={message_id} from={from_number} "
        f"type={message_type} ts={timestamp} body={text_body!r}"
    )


def _process_status(status: Dict[str, Any]) -> None:
    status_id = status.get("id")
    recipient = status.get("recipient_id")
    status_value = status.get("status")
    timestamp = status.get("timestamp")
    conversation = (status.get("conversation") or {}).get("origin", {}).get("type")

    errors = None
    err_list = status.get("errors") or []
    if err_list:
        errors = "; ".join(
            f"#{e.get('code')} {e.get('title')}" for e in err_list
        ) or None

    print(
        f"[STATUS] id={status_id} to={recipient} "
        f"status={status_value} origin={conversation!r} ts={timestamp}"
        + (f" errors=[{errors}]" if errors else "")
    )


async def send_whatsapp_text(
    to: str,
    text: str,
    *,
    phone_number_id: Optional[str] = None,
    preview_url: bool = True,
    reply_to_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not ACCESS_TOKEN:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN must be set")
    resolved_pnid = phone_number_id or PHONE_NUMBER_ID
    if not resolved_pnid:
        raise RuntimeError(
            "phone_number_id must be provided or WHATSAPP_PHONE_NUMBER_ID set"
        )

    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": preview_url, "body": text},
    }
    if reply_to_message_id:
        payload["context"] = {"message_id": reply_to_message_id}

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    url = _graph_url_for(resolved_pnid)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        body = r.json() if r.content else {}
        if 200 <= r.status_code < 300:
            return body
        raise RuntimeError(
            f"WhatsApp API {r.status_code} (pnid={resolved_pnid}): "
            f"{body.get('error', {}).get('message', r.text)}"
        )


async def _echo_reply(
    *,
    from_number: str,
    phone_number_id: str,
    inbound_text: Optional[str],
    reply_to_message_id: Optional[str],
) -> None:
    if not from_number or not phone_number_id:
        return

    static_reply = (
        "Hello! Message received."
        if not inbound_text
        else f"Hello! Message received. You said: {inbound_text}"
    )

    try:
        result = await send_whatsapp_text(
            to=from_number,
            text=static_reply,
            phone_number_id=phone_number_id,
            reply_to_message_id=reply_to_message_id,
            preview_url=False,
        )
        messages = result.get("messages") or []
        sent_id = messages[0].get("id") if messages else None
        print(
            f"[ECHO OK] to={from_number} pnid={phone_number_id} "
            f"in_reply_to={reply_to_message_id} sent_id={sent_id}"
        )
    except Exception as exc:  # pragma: no cover - best effort, never break 200
        print(
            f"[ECHO FAIL] to={from_number} pnid={phone_number_id} "
            f"error={exc!r}"
        )


async def _llm_reply(
    *,
    from_number: str,
    phone_number_id: str,
    inbound_text: Optional[str],
    reply_to_message_id: Optional[str],
) -> None:
    """Petrobind AI assistant reply (RAG + tools + guardrails). On any failure
    falls back to the _echo_reply path so the buyer always gets a 200-receipt
    plus a message back."""
    if not from_number:
        return

    try:
        reply_text = await llm_assistant.handle_incoming_message(
            phone_number=from_number,
            inbound_text=inbound_text or "",
        )
    except Exception as exc:  # pragma: no cover - must never break Meta 200
        print(f"[LLM FAIL] from={from_number!r} error={exc!r} -> falling back to echo")
        await _echo_reply(
            from_number=from_number,
            phone_number_id=phone_number_id,
            inbound_text=inbound_text,
            reply_to_message_id=reply_to_message_id,
        )
        return

    if not reply_text:
        reply_text = (
            "Thanks for your message to Petrobind Global — a member of the "
            "trading desk will reply to you shortly."
        )

    try:
        result = await send_whatsapp_text(
            to=from_number,
            text=reply_text,
            phone_number_id=phone_number_id or PHONE_NUMBER_ID,
            reply_to_message_id=reply_to_message_id,
            preview_url=True,
        )
        messages = result.get("messages") or []
        sent_id = messages[0].get("id") if messages else None
        sess = conversation_store._SESSIONS.get(from_number)
        inquiry = sess.inquiry.as_dict() if sess and sess.inquiry else {}
        inquiry_preview = {k: v for k, v in inquiry.items() if v}
        print(
            f"[LLM OK] to={from_number} pnid={phone_number_id} sent_id={sent_id} "
            f"lead_notified={sess.lead_notified if sess else None} "
            f"handoff_notified={sess.handoff_notified if sess else None} "
            f"inquiry={inquiry_preview!r} reply={reply_text[:120]!r}"
        )
    except Exception as exc:  # pragma: no cover - best effort send
        print(f"[LLM SEND FAIL] to={from_number!r} error={exc!r}")


async def _instant_handoff_reply(
    *,
    from_number: str,
    phone_number_id: str,
    reply_to_message_id: Optional[str],
    reason: str,
) -> None:
    """Deterministic, code-side sales handoff — ZERO latency, NO model involved.

    Used when the buyer types P0.4 instant-escalation keywords ("human", "urgent",
    "manager", etc.) or when a non-text media message arrives. Always sends
    (a) a short WhatsApp confirmation + (b) the handoff email in parallel.
    Never raises — best-effort.
    """
    reply = (
        "Of course — a Petrobind trading specialist has been paged directly "
        "and will be with you here on WhatsApp within the next business hours. "
        "To make the conversation faster, feel free to reply in the meantime "
        "with: the target product (e.g. Bitumen 60/70), destination port, "
        "and approximate volume per shipment. Thank you!"
    )
    try:
        res = await send_whatsapp_text(
            to=from_number,
            text=reply,
            phone_number_id=phone_number_id or PHONE_NUMBER_ID,
            reply_to_message_id=reply_to_message_id,
            preview_url=False,
        )
        sent_id = (res.get("messages") or [{}])[0].get("id")
        print(
            f"[INSTANT HANDOFF OK] to={from_number!r} pnid={phone_number_id!r} "
            f"sent_id={sent_id!r} reason={reason[:80]!r}"
        )
    except Exception as exc:
        print(f"[INSTANT HANDOFF SEND FAIL] to={from_number!r} error={exc!r}")

    try:
        sess = conversation_store.get_session(from_number) if from_number else None
        if sess and not sess.handoff_notified:
            inquiry_dict = sess.inquiry.as_dict() if sess else {}
            ok = await notify.send_handoff_email(
                phone_number=from_number,
                reason=reason,
                partial_inquiry_summary=(
                    "Instant keyword-triggered handoff.\n"
                    f"Inquiry fields so far: {json.dumps({k:v for k,v in inquiry_dict.items() if v})}"
                ),
            )
            if ok and sess:
                sess.handoff_notified = True
    except Exception as exc:
        print(f"[INSTANT HANDOFF EMAIL FAIL] to={from_number!r} error={exc!r}")


# ---------------- Cal.com booking webhook ----------------

CAL_COM_WEBHOOK_SECRET = os.getenv("CAL_COM_WEBHOOK_SECRET") or ""


@app.post("/cal-webhook")
async def cal_webhook(request: Request) -> JSONResponse:
    """Cal.com booking notification webhook.

    Verifies X-Cal-Signature-256 if CAL_COM_WEBHOOK_SECRET env is set, then
    passes the payload to booking.handle_cal_webhook which fires a
    notify.send_booking_email on BOOKING_CREATED triggers.

    Always returns 200 (Cal.com retries on non-2xx).
    """
    raw = await request.body()
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if CAL_COM_WEBHOOK_SECRET:
        import hashlib
        import hmac

        provided = request.headers.get("x-cal-signature-256") or ""
        expected = (
            "sha256="
            + hmac.new(
                CAL_COM_WEBHOOK_SECRET.encode("utf-8"),
                msg=raw or b"",
                digestmod=hashlib.sha256,
            ).hexdigest()
        )
        if not provided or not hmac.compare_digest(provided, expected):
            print(f"[CAL-WEBHOOK] signature mismatch: got={provided[:20]!r}...")
            return JSONResponse(
                content={"status": "ignored", "detail": "invalid signature"},
                status_code=status.HTTP_200_OK,
            )

    body_text, status_code = await booking.handle_cal_webhook(payload)
    return JSONResponse(content={"status": body_text}, status_code=status_code)


# ---------------- Idle-session follow-up cron endpoint ----------------

FOLLOWUPS_CRON_TOKEN = os.getenv("FOLLOWUPS_CRON_TOKEN") or ""
# Max nudges per cron run (bounds outbound WhatsApp per hour — prevents spam
# if someone floods the endpoint). You can raise this after seeing throughput.
MAX_NUDGES_PER_RUN = int(os.getenv("FOLLOWUPS_MAX_NUDGES_PER_RUN", "50")) or 50


@app.post("/followups-scan")
async def followups_scan(request: Request) -> JSONResponse:
    """Periodic cron trigger: send 1 nudge per idle session that needs one.

    Trigger this endpoint from an external cron every 1-6 hours:
      - Render Cron jobs (paid): https://dashboard.render.com/cron
      - cron-job.org (free): POST to this URL with the bearer token below
      - Local dev: `curl -X POST -H 'Authorization: Bearer $TOKEN' …/followups-scan`

    Optional (recommended) auth: set FOLLOWUPS_CRON_TOKEN env (a long random
    string, e.g. `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`).
    Without it the endpoint still runs but prints a WARN. We never send more
    than FOLLOWUPS_MAX_NUDGES_PER_RUN (default 50) in a single call.

    Returns: {pruned:int, scanned:int, sent:int, failed:int, actions:[...]}
    """
    # Optional bearer auth (best-effort — no crash if token is not set yet).
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    token_good = True
    if FOLLOWUPS_CRON_TOKEN:
        expected = f"Bearer {FOLLOWUPS_CRON_TOKEN}"
        token_good = (
            bool(auth)
            and len(auth) == len(expected)
            and auth[:7] == "Bearer "
            and auth[7:] == FOLLOWUPS_CRON_TOKEN
        )
        if not token_good:
            return JSONResponse(
                content={"status": "unauthorized", "detail": "bad FOLLOWUPS_CRON_TOKEN"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
    else:
        print("[FOLLOWUPS] WARN: FOLLOWUPS_CRON_TOKEN env not set — anyone can trigger this. Set a long random secret.")

    pruned = conversation_store.prune_old_sessions()
    actions = conversation_store.scan_for_followups()
    actions = actions[:MAX_NUDGES_PER_RUN]

    sent = 0
    failed = 0
    results: list[dict] = []
    for act in actions:
        try:
            res = await send_whatsapp_text(
                to=act.phone_number,
                text=act.message_text,
                preview_url=False,
            )
            conversation_store.mark_followup_sent(act.phone_number, act.kind)
            sent += 1
            results.append({
                **act.as_dict(),
                "sent": True,
                "sent_id": (res.get("messages") or [{}])[0].get("id"),
            })
        except Exception as exc:  # pragma: no cover - best-effort per-nudge
            failed += 1
            results.append({**act.as_dict(), "sent": False, "error": repr(exc)})

    scanned_sessions = len(conversation_store.all_sessions())
    print(
        f"[FOLLOWUPS] pruned={pruned} scanned_sessions={scanned_sessions} "
        f"actions={len(actions)} sent={sent} failed={failed}"
    )
    return JSONResponse(
        content={
            "pruned": pruned,
            "scanned_sessions": scanned_sessions,
            "actions_returned": len(actions),
            "sent": sent,
            "failed": failed,
            "actions": results,
        },
        status_code=status.HTTP_200_OK,
    )


@app.post("/send-message")
async def send_message_endpoint(request: Request) -> JSONResponse:
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    to = body.get("to")
    text = body.get("text")
    if not to or not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request must include 'to' (phone in E.164) and 'text' (string)",
        )

    reply_to = body.get("reply_to_message_id")
    override_pnid = body.get("phone_number_id")

    try:
        result = await send_whatsapp_text(
            to=str(to),
            text=str(text),
            phone_number_id=str(override_pnid) if override_pnid else None,
            preview_url=bool(body.get("preview_url", True)),
            reply_to_message_id=str(reply_to) if reply_to else None,
        )
    except RuntimeError as exc:
        return JSONResponse(
            content={"success": False, "error": str(exc)},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    return JSONResponse(
        content={"success": True, "result": result},
        status_code=status.HTTP_200_OK,
    )


@app.api_route(
    "/{path_name:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def probe_catchall(request: Request, path_name: str) -> JSONResponse:
    if path_name in {"", "docs", "redoc", "openapi.json"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    print(f"[PROBE] {request.method} /{path_name} - returned 200 for Meta probe")
    return JSONResponse(
        content={"status": "ok", "path": f"/{path_name}"},
        status_code=status.HTTP_200_OK,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
