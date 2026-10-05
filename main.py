import asyncio
from dataclasses import asdict
import datetime
import json
import os
import time
from collections import deque as _rate_deque
from pydantic import BaseModel
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv

load_dotenv()

import booking
import contact_names
import feedback_store
import learning
import conversation_store
import httpx
import llm_assistant
import scheduler
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

try:
    import supabase_client as _sb  # new shared-inbox persistence; optional
except Exception:  # pragma: no cover - file may not exist yet during hot edits
    _sb = None  # type: ignore
try:
    import base64          # used by fallback cookie signer; stdlib always present
    import secrets         # used by claim acquire + human-send REST endpoints; stdlib
    import secrets as _secrets
    import base64 as _base64
    import hashlib as _hashlib
    import hmac as _hmac
except Exception:  # pragma: no cover - stdlib, always present
    _secrets = None  # type: ignore

# --- Jinja2 + itsdangerous for shared-inbox HTML UI (optional, safe no-op if missing) ---
# V1 frontend: Jinja2 templates + HTMX 2.0 (CDN) in SAME FastAPI process as the
# webhook → 1 Render service, no cross-service latency, no extra env duplication.
_JINJA_ENV = None
_JINJA_TEMPLATE_ERROR: Optional[str] = None
_ITS_DANGEROUS_OK: bool = False
_URL_SAFE_SERIALIZER = None
try:
    from jinja2 import Environment as _JinjaEnv, FileSystemLoader as _FSLoader, select_autoescape as _select_autoescape  # type: ignore
    import os as _jinja_os
    _template_dir = _jinja_os.path.join(_jinja_os.path.dirname(_jinja_os.path.abspath(__file__)), "petrobind_frontend_app", "templates")
    if _jinja_os.path.isdir(_template_dir):
        _JINJA_ENV = _JinjaEnv(
            loader=_FSLoader(_template_dir),
            autoescape=_select_autoescape(["html", "xml"]),
        )
    else:
        _JINJA_TEMPLATE_ERROR = f"Template directory not found: {_template_dir!r}"
except Exception as _jinja_exc:  # pragma: no cover - jinja2 is in requirements.txt but tolerate absence
    _JINJA_TEMPLATE_ERROR = f"Jinja2 import failed: {type(_jinja_exc).__name__}: {_jinja_exc!s}"
try:
    from itsdangerous import URLSafeTimedSerializer as _Serializer  # type: ignore
    if len(_INBOX_COOKIE_SIGNING_KEY) >= 8:
        _URL_SAFE_SERIALIZER = _Serializer(_INBOX_COOKIE_SIGNING_KEY, salt="petrobind-inbox-v1")
        _ITS_DANGEROUS_OK = True
except Exception as _its_exc:  # pragma: no cover - itsdangerous in requirements.txt
    _ITS_DANGEROUS_OK = False
    if _JINJA_TEMPLATE_ERROR is None:
        _JINJA_TEMPLATE_ERROR = f"itsdangerous import failed: {type(_its_exc).__name__}: {_its_exc!s}"

from rag import load_index_if_needed

app = FastAPI(title="WhatsApp Webhook Gateway", version="1.1.0")

# --- new shared-inbox env (Petrobind Global only, optional) ---
# INBOX_ADMIN_TOKEN gates both /api/inbox/* (bearer) AND the /inbox/* HTML pages.
INBOX_ADMIN_TOKEN: str = os.getenv("INBOX_ADMIN_TOKEN") or ""
INBOX_ADMIN_NAME: str = os.getenv("INBOX_ADMIN_NAME") or "Petrobind Admin"
INBOX_SESSION_EXPIRE_MINUTES: int = int(os.getenv("INBOX_SESSION_EXPIRE_MINUTES", "60") or "60") or 60
# Cheap unsigned login session cookie for V1 (single-admin). Signing prevents
# a visitor editing the cookie manually; not a full auth stack.
_INBOX_COOKIE_SIGNING_KEY: bytes = (
    os.getenv("INBOX_COOKIE_SIGNING_KEY")
    or (INBOX_ADMIN_TOKEN + "___jarvis-petrobind-inbox-v1")
).encode("utf-8")[:64] or b"jarvis-petrobind-inbox-v1-default-key-change-me"

TEMPLATES_CACHE: Optional[Tuple[float, List[Dict[str, Any]]]] = None
TEMPLATES_CACHE_TTL = 300


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


def make_rate_limiter(
    per_minute_limit: int, per_key: bool = False
) -> Callable[[str], Tuple[bool, float]]:
    _buckets: Dict[str, _rate_deque[float]] = {}
    _global_bucket: _rate_deque[float] = _rate_deque(maxlen=per_minute_limit * 10)

    def _check(key: str = "global") -> Tuple[bool, float]:
        now = time.time()
        cutoff = now - 60.0
        if per_key:
            bucket = _buckets.get(key)
            if bucket is None:
                bucket = _rate_deque(maxlen=per_minute_limit * 10)
                _buckets[key] = bucket
        else:
            bucket = _global_bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= per_minute_limit:
            retry_after = 60.0 - (now - bucket[0]) if bucket else 1.0
            return False, max(0.1, retry_after)
        bucket.append(now)
        return True, 0.0

    return _check


_per_e164_rate_limiter = make_rate_limiter(5, per_key=True)
_global_proactive_rate_limiter = make_rate_limiter(200, per_key=False)


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
        "Thanks, I've received your document and forwarded it to the "
        "Petrobind sales director for their review. They'll reply directly "
        "here with any comments. Quick clarifier: is this for a specific "
        "product grade (e.g. Bitumen 60/70) and destination port?"
    ),
    "image": (
        "Got the image and passed it to our sales director. If this is a photo "
        "of a delivery/QC issue, just reply with the product, shipment date, "
        "and any notes and the team can action it. Otherwise: which grade or "
        "product is it related to?"
    ),
    "audio": (
        "Got your voice note, I've flagged it for the Petrobind sales director "
        "and they'll listen and revert. To speed things up: is it regarding a "
        "quote request, logistics, or an existing shipment?"
    ),
    "voice": (
        "Got your voice note, flagged it for the sales director. To speed "
        "things up: is it regarding a quote request, logistics, or an existing "
        "shipment?"
    ),
    "video": (
        "Got the video, forwarded to the sales director. One quick thing: "
        "which product and destination port is this for, so the right person "
        "reviews it first?"
    ),
    "sticker": "",  # ignore — no reply needed (silent receipt)
    "reaction": "",  # ignore — no reply needed
    "contacts": (
        "Got the contact card, forwarded to our sales director. Quick question: which "
        "Petrobind product and destination port should we associate with this "
        "contact?"
    ),
    "location": (
        "Got the location. If this is a destination port or a pickup point, "
        "let me know the target product + volume and I'll have the sales director prep "
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
    email_ok = bool(
        os.getenv("GMAIL_RELAY_URL")
        or (os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD") and os.getenv("SALES_EMAIL"))
    )
    supabase_ok = _sb_enabled()
    inbox_ok = bool(INBOX_ADMIN_TOKEN)
    print(
        "[STARTUP] WhatsApp tokens OK. "
        f"phone_number_id={_mask(PHONE_NUMBER_ID)!r} "
        f"OPENAI_API_KEY={'set' if os.getenv('OPENAI_API_KEY') else '<unset>'} "
        f"OPENROUTER_API_KEY={'set' if openrouter_ok else '<unset — LLM replies will use canned fallback>'} "
        f"EMAIL={'configured' if email_ok else '<unconfigured — lead/handoff emails will be SKIPPED>'} "
        f"WHATSAPP_APP_SECRET={'set' if WHATSAPP_APP_SECRET else '<unset — /webhook signature NOT verified, security risk>'} "
        f"CAL_COM_BOOKING_LINK={'set' if booking.is_configured() else '<unset>'} "
        f"CAL_COM_WEBHOOK_SECRET={'set' if CAL_COM_WEBHOOK_SECRET else '<unset — /cal-webhook signature NOT verified>'} "
        f"FOLLOWUPS_CRON_TOKEN={'set' if FOLLOWUPS_CRON_TOKEN else '<unset — /followups-scan will WARN each call>'} "
        f"SUPABASE_INBOX={'ready' if supabase_ok else '<disabled — set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY to enable>'} "
        f"INBOX_LOGIN={'ENABLED' if inbox_ok else '<set INBOX_ADMIN_TOKEN to gate /inbox/* UI>'} "
    )
    try:
        load_index_if_needed()
    except Exception as exc:  # pragma: no cover - best effort warmup
        print(f"[WARN] rag index warmup failed (continuing): {exc!r}")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
# Meta App Dashboard -> Settings -> Basic -> App Secret. Used to verify
# X-Hub-Signature-256 on inbound /webhook POSTs (see receive_webhook).
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
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

# --- shared-inbox enabled? (Supabase DB up + optional env set) ---
def _sb_enabled() -> bool:
    return bool(_sb is not None and _sb.ENABLED)  # type: ignore[attr-defined]


async def _persist_inbound_safe(msg: Dict[str, Any]) -> None:
    """Fire-and-forget persistence hook; 2 s timeout, 1 WARN line, never raises."""
    if not _sb_enabled():
        return
    try:
        async with asyncio.timeout(2.0):  # type: ignore[attr-defined]
            await _sb.insert_inbound_message(msg)  # type: ignore[attr-defined]
    except Exception as exc:
        wamid = msg.get("id")
        print(
            f"[PERSIST-WARN] inbound wamid={wamid!r} save failed: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )


async def _persist_outbound_safe(
    *,
    e164: str,
    direction: str,
    text: str,
    reply_to_wamid: Optional[str] = None,
    sent_id_from_graph: Optional[str] = None,
    errored: bool = False,
    error_detail: Optional[str] = None,
) -> None:
    if not _sb_enabled():
        return
    try:
        async with asyncio.timeout(2.0):  # type: ignore[attr-defined]
            await _sb.insert_outbound_message(  # type: ignore[attr-defined]
                e164=e164,
                direction=direction,
                text=text,
                reply_to_wamid=reply_to_wamid,
                sent_id_from_graph=sent_id_from_graph,
                errored=errored,
                error_detail=error_detail,
            )
    except Exception as exc:
        print(
            f"[PERSIST-WARN] outbound save to={e164!r} dir={direction!r} failed: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )


async def _claim_is_held_by_other(e164: str, my_session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """If a chat is claimed by someone OTHER than my_session_id, return their info dict.

    Used by AI reply paths BEFORE actually sending WhatsApp. If held → AI does NOT send,
    just logs and persists a system-note row.
    """
    if not _sb_enabled() or not e164:
        return None
    try:
        info = await _sb.claim_is_human_held(e164)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not info:
        return None
    if my_session_id and info.get("session_id") == my_session_id:
        return None  # same browser session
    return info


def _bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


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
            "version": "1.1.0",
            "verify_token_loaded": bool(VERIFY_TOKEN),
            "verify_token_preview": debug_token,
            "phone_number_id_loaded": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
            "access_token_loaded": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")),
            "privacy_policy_url": "/privacy-policy",
            "terms_of_service_url": "/terms-of-service",
            "app_icon_url": "/app-icon.svg",
            "shared_inbox_enabled": _sb_enabled(),
            "inbox_login": bool(INBOX_ADMIN_TOKEN),
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Always-available health endpoint — Render auto-pings this.

    Return value is intentionally descriptive, but HTTP status is always 200
    (Render's health check considers anything non-2xx a crash and rolls back
    deploys; we never want a Supabase-downtime side-effect to roll back the
    WhatsApp webhook gateway service itself — that would lose buyer messages.)
    """
    rag_ok = True
    try:
        rag_ok = bool(rag.index_is_ready()) if callable(getattr(rag, "index_is_ready", None)) else True
    except Exception:
        rag_ok = True
    redis_scan_available = False
    try:
        redis_env_ok = bool(os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"))
        cs_last_scan = getattr(conversation_store, "_last_redis_full_scan_ts", None)
        redis_scan_available = (cs_last_scan is not None) or redis_env_ok
    except Exception:
        redis_scan_available = False
    followups_configured = False
    try:
        followups_configured = bool(os.getenv("FOLLOWUPS_CRON_TOKEN", "").strip())
    except Exception:
        followups_configured = False
    checks: Dict[str, Any] = {
        "whatsapp_env": bool(ACCESS_TOKEN and PHONE_NUMBER_ID),
        "llm_ready": bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")),
        "rag_index_ready": rag_ok,
        "supabase_inbox": _sb_enabled(),
        "inbox_login_configured": bool(INBOX_ADMIN_TOKEN),
        "booking_configured": booking.is_configured(),
        "email_configured": bool(
            os.getenv("GMAIL_RELAY_URL")
            or (os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD") and os.getenv("SALES_EMAIL"))
        ),
        "redis_scan_available": redis_scan_available,
        "followups_configured": followups_configured,
    }
    return JSONResponse(
        content={"status": "ok", "app": APP_NAME, "version": "1.1.0", "checks": checks},
        status_code=status.HTTP_200_OK,
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

    # Verify Meta's X-Hub-Signature-256 when WHATSAPP_APP_SECRET is set —
    # without this, ANY caller who knows this URL can POST fake WhatsApp
    # messages: real LLM API cost per message, spam lead/handoff emails,
    # and outbound WhatsApp sends billed to this business number, all
    # triggered by a spoofed payload with no proof it came from Meta.
    # Mirrors the same pattern already used for /cal-webhook below.
    if WHATSAPP_APP_SECRET:
        import hashlib
        import hmac

        provided = request.headers.get("x-hub-signature-256") or ""
        expected = (
            "sha256="
            + hmac.new(WHATSAPP_APP_SECRET.encode("utf-8"), msg=raw or b"", digestmod=hashlib.sha256).hexdigest()
        )
        if not provided or not hmac.compare_digest(provided, expected):
            print(f"[WEBHOOK] signature mismatch, rejecting: got={provided[:20]!r}...")
            # 200 (not 401/403): Meta's webhook delivery retries aggressively
            # on non-2xx, which would just hammer us with the same forged
            # or misconfigured request. Silently drop instead.
            return JSONResponse(content={"status": "ignored"}, status_code=status.HTTP_200_OK)
    else:
        print("[WEBHOOK] WARN: WHATSAPP_APP_SECRET not set — signature NOT verified, anyone can POST fake messages here.")

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
                        _schedule_batched_reply(
                            from_number=message.get("from"),
                            phone_number_id=pnid,
                            text=text_body or "",
                            reply_to_message_id=wamid,
                        )
                        continue

                    # P0.2 Non-text inbound media messages.
                    _process_message(message)
                    canned = _NONTEXT_MEDIA_REPLY.get(msg_type)
                    summary_extra = ""
                    if canned:
                        # --- claim mutex (additive, no break): if human holds claim, skip canned reply ---
                        # NOTE: Handoff email still fires below this block so the human
                        # sees the inbound media alert; only the canned text auto-reply to
                        # the buyer is suppressed.
                        media_from = message.get("from")
                        held = await _claim_is_held_by_other(media_from) if media_from else None
                        if held:
                            print(
                                f"[MEDIA ACK SKIP — human holds claim] type={msg_type!r} "
                                f"from={media_from!r} held_by={held.get('held_by')!r}"
                            )
                            asyncio.create_task(_persist_outbound_safe(
                                e164=media_from or "",
                                direction="system",
                                text=(
                                    f"[AI skipped media-canned send (claim held by {held.get('held_by')}): "
                                    f"Inbound {msg_type} received. Canned would have said: "
                                    f"{canned[:300]}{'…' if len(canned) > 300 else ''}]"
                                ),
                                reply_to_wamid=wamid,
                            ))
                            summary_extra = (
                                f"Buyer sent a WhatsApp {msg_type} message (msg_id={wamid}). "
                                f"Canned auto-reply SKIPPED (chat held by {held.get('held_by')}). "
                                f"Please reply to them manually in the shared inbox."
                            )
                        else:
                            try:
                                res = await send_whatsapp_text(
                                    to=media_from,
                                    text=canned,
                                    phone_number_id=pnid,
                                    reply_to_message_id=wamid,
                                    preview_url=False,
                                    sender_direction="ai",
                                )
                                sent_id = (res.get("messages") or [{}])[0].get("id")
                                print(
                                    f"[MEDIA ACK OK] type={msg_type!r} from={media_from!r} "
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
                            media_from = message.get("from") or ""
                            media_sess = await conversation_store.get_session(media_from) if media_from else None
                            await notify.send_handoff_email(
                                phone_number=media_from,
                                reason=(
                                    f"Inbound {msg_type} message received (no auto-processing)."
                                ),
                                partial_inquiry_summary=summary_extra,
                                relationship_summary=media_sess.relationship_summary() if media_sess else None,
                                recent_transcript=media_sess.recent_transcript() if media_sess else None,
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
    # --- additive Supabase persistence hook (no breaking path) ---
    asyncio.create_task(_persist_inbound_safe(message))


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
    # --- additive Supabase hook (no breaking) ---
    asyncio.create_task(_sb.mark_status(status_id, recipient, status_value, errors) if _sb_enabled() else None)


async def send_whatsapp_text(
    to: str,
    text: str,
    *,
    phone_number_id: Optional[str] = None,
    preview_url: bool = True,
    reply_to_message_id: Optional[str] = None,
    sender_direction: str = "ai",   # NEW: "ai" | "human" | "system" for inbox thread
) -> Dict[str, Any]:
    """Send a WhatsApp text via Cloud API Graph.

    sender_direction (new, default "ai"): used ONLY by the Supabase shared-inbox
    persistence hook so the thread UI labels outbound messages correctly.
    Petrobind legacy callers that don't pass it get "ai" as default, which is
    correct (most of the gateway is AI-replying). The /api/inbox/chats/{e164}/messages
    human-send endpoint passes sender_direction="human".
    """
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
            sent_id = (body.get("messages") or [{}])[0].get("id") if body else None
            # --- additive: persist successful send to shared inbox DB ---
            asyncio.create_task(_persist_outbound_safe(
                e164=to,
                direction=sender_direction,
                text=text,
                reply_to_wamid=reply_to_message_id,
                sent_id_from_graph=sent_id,
            ))
            return body
        # --- additive: persist failed send with errored flag ---
        asyncio.create_task(_persist_outbound_safe(
            e164=to,
            direction=sender_direction,
            text=text,
            reply_to_wamid=reply_to_message_id,
            errored=True,
            error_detail=str(body.get("error", {}).get("message", r.text))[:500],
        ))
        raise RuntimeError(
            f"WhatsApp API {r.status_code} (pnid={resolved_pnid}): "
            f"{body.get('error', {}).get('message', r.text)}"
        )


async def send_whatsapp_template(
    recipient_e164: str,
    template_name: str,
    language_code: str = "en_US",
    params: Optional[Union[List[Any], Dict[str, Any]]] = None,
    *,
    phone_number_id: Optional[str] = None,
    sender_direction: str = "ai",
    created_by: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    if not ACCESS_TOKEN:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN must be set")
    resolved_pnid = phone_number_id or PHONE_NUMBER_ID
    if not resolved_pnid:
        raise RuntimeError(
            "phone_number_id must be provided or WHATSAPP_PHONE_NUMBER_ID set"
        )

    template: Dict[str, Any] = {
        "name": template_name,
        "language": {"policy": "deterministic", "code": language_code},
    }
    if params is not None:
        param_list: List[Any]
        if isinstance(params, list):
            param_list = list(params)
        elif isinstance(params, dict):
            param_list = [params[k] for k in sorted(params.keys())]
        else:
            param_list = []
        if param_list:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(v)} for v in param_list
                    ],
                }
            ]

    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": recipient_e164,
        "type": "template",
        "template": template,
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    url = _graph_url_for(resolved_pnid)

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        body = r.json() if r.content else {}
        if 200 <= r.status_code < 300:
            messages = body.get("messages") or []
            sent_id = messages[0].get("id") if messages else None
            cat_log = category if category else "UNKNOWN"
            print(
                f"[TEMPLATE-SEND] name={template_name} category={cat_log} "
                f"e164={recipient_e164} sent_id={sent_id or 'NONE'}"
            )
            asyncio.create_task(_persist_outbound_safe(
                e164=recipient_e164,
                direction=sender_direction,
                text=f"[template {template_name} {language_code}]",
                sent_id_from_graph=sent_id,
            ))
            return {
                "ok": True,
                "messages": messages,
                "meta_http_status": r.status_code,
                "raw": body,
            }
        asyncio.create_task(_persist_outbound_safe(
            e164=recipient_e164,
            direction=sender_direction,
            text=f"[template {template_name} {language_code}]",
            errored=True,
            error_detail=str(body.get("error", {}).get("message", r.text))[:500],
        ))
        raise RuntimeError(
            f"WhatsApp Template API {r.status_code} (pnid={resolved_pnid}): "
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

    # --- claim mutex (additive, no break): if human holds claim, skip actual send ---
    held = await _claim_is_held_by_other(from_number)
    if held:
        print(
            f"[ECHO SKIP — human holds claim] to={from_number!r} held_by={held.get('held_by')!r} "
            f"expires_in={held.get('expires_in_secs')}s"
        )
        asyncio.create_task(_persist_outbound_safe(
            e164=from_number,
            direction="system",
            text=(
                f"[AI skipped send: chat held by {held.get('held_by')} (human claim). "
                f"Echo would have been: Hello! Message received"
                + (f". You said: {inbound_text}" if inbound_text else "")
                + "]"
            ),
            reply_to_wamid=reply_to_message_id,
        ))
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
            sender_direction="ai",
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


# ---------------------- Message batching / debounce -------------------------
# A buyer often sends 2-3 quick separate WhatsApp messages instead of one long
# one (typing habit). Replying to each individually reads as robotic and can
# misfire the LLM's turn-taking logic on a fragment ("100", then "MT", then
# "per month"). Instead we hold each text message for DEBOUNCE_SECONDS after
# the first one arrives, keep collecting any more that show up from the same
# sender in that window, and once it goes quiet, process everything as ONE
# combined turn. In-process only (same lifetime as everything else in
# conversation_store), and does not delay Meta's webhook ack — the wait
# happens in a background asyncio task, not in the request/response cycle.
DEBOUNCE_SECONDS = 8

_pending_texts: dict[str, list[str]] = {}
_pending_generation: dict[str, int] = {}
_pending_meta: dict[str, dict] = {}


def _schedule_batched_reply(
    *, from_number: str, phone_number_id: str, text: str, reply_to_message_id: Optional[str]
) -> None:
    """Register one inbound text message for the buyer and (re)start the
    debounce timer. Does not block — safe to call from the webhook handler."""
    if not from_number or not text:
        return
    _pending_texts.setdefault(from_number, []).append(text)
    generation = _pending_generation.get(from_number, 0) + 1
    _pending_generation[from_number] = generation
    _pending_meta[from_number] = {
        "phone_number_id": phone_number_id,
        "reply_to_message_id": reply_to_message_id,
    }
    asyncio.create_task(_debounced_flush(from_number, generation))


async def _debounced_flush(from_number: str, generation: int) -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    # If a newer message arrived while we were sleeping, its own task owns
    # the flush now (bumped the generation) — this one is stale, do nothing.
    if _pending_generation.get(from_number) != generation:
        return
    texts = _pending_texts.pop(from_number, [])
    meta = _pending_meta.pop(from_number, {})
    if not texts:
        return
    combined = "\n".join(texts)
    print(
        f"[BATCH] flushing {len(texts)} message(s) from {from_number!r} "
        f"after {DEBOUNCE_SECONDS}s quiet period"
    )
    await _llm_reply(
        from_number=from_number,
        phone_number_id=meta.get("phone_number_id") or PHONE_NUMBER_ID,
        inbound_text=combined,
        reply_to_message_id=meta.get("reply_to_message_id"),
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

    # --- claim mutex (additive, no break): if human holds claim, skip actual send ---
    # NOTE: We still run the LLM pipeline to populate lead/handoff emails and
    # session state (those side effects still happen). We only skip the
    # WhatsApp Graph send so the human sees the AI's would-have-sent text
    # logged + persisted as a system note in the thread UI.
    held = await _claim_is_held_by_other(from_number)

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
            "Thanks for your message, let me get back to you on that shortly."
        )

    if held:
        print(
            f"[AI SKIP — human holds claim] to={from_number!r} held_by={held.get('held_by')!r} "
            f"expires_in={held.get('expires_in_secs')}s reply_preview={reply_text[:200]!r}"
        )
        asyncio.create_task(_persist_outbound_safe(
            e164=from_number,
            direction="system",
            text=(
                f"[AI skipped llm_reply send (claim held by {held.get('held_by')}): "
                f"{reply_text[:400]}{'…' if len(reply_text) > 400 else ''}]"
            ),
            reply_to_wamid=reply_to_message_id,
        ))
        return

    try:
        result = await send_whatsapp_text(
            to=from_number,
            text=reply_text,
            phone_number_id=phone_number_id or PHONE_NUMBER_ID,
            reply_to_message_id=reply_to_message_id,
            preview_url=True,
            sender_direction="ai",
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
    (a) a short WhatsApp confirmation, offering to book a call with the buyer
    when they're free, + (b) the handoff email to admin in parallel.
    Never raises — best-effort.
    """
    sess = await conversation_store.get_session(from_number) if from_number else None
    if booking.is_configured() and sess and not sess.booking_link_shared_at:
        reply = (
            "Of course, let me get back to you on that shortly. If you're "
            "free, it's often quickest to grab a short call with our sales "
            "director: " + booking.get_booking_link()
        )
        sess.booking_link_shared_at = time.time()
        if not sess.booking_intent_notified:
            try:
                ok = await notify.send_booking_interest_email(
                    phone_number=from_number,
                    relationship_summary=sess.relationship_summary(),
                    recent_transcript=sess.recent_transcript(),
                )
                if ok:
                    sess.booking_intent_notified = True
            except Exception as exc:
                print(f"[BOOKING INTEREST EMAIL FAIL] to={from_number!r} error={exc!r}")
    else:
        reply = (
            "Of course, let me get back to you on that shortly. In the meantime, "
            "it'll help if you can share the target product (e.g. Bitumen "
            "60/70), destination port, and roughly how much volume you need per "
            "shipment. Thank you!"
        )

    # --- claim mutex (additive, no break): if human holds claim, skip actual WhatsApp send ---
    # NOTE: Handoff/booking-interest EMAILS still happen (they already ran above)
    # so the human sees the escalation; only the automated reply to the buyer
    # is skipped so the human in the tab writes their own.
    held = await _claim_is_held_by_other(from_number)
    if held:
        print(
            f"[INSTANT HANDOFF SKIP — human holds claim] to={from_number!r} "
            f"held_by={held.get('held_by')!r} expires_in={held.get('expires_in_secs')}s "
            f"reason={reason[:60]!r}"
        )
        asyncio.create_task(_persist_outbound_safe(
            e164=from_number,
            direction="system",
            text=(
                f"[AI skipped instant-handoff send (claim held by {held.get('held_by')}). "
                f"Reason: {reason[:200]}. Would have replied: {reply[:300]}{'…' if len(reply) > 300 else ''}]"
            ),
            reply_to_wamid=reply_to_message_id,
        ))
    else:
        try:
            res = await send_whatsapp_text(
                to=from_number,
                text=reply,
                phone_number_id=phone_number_id or PHONE_NUMBER_ID,
                reply_to_message_id=reply_to_message_id,
                preview_url=False,
                sender_direction="ai",
            )
            sent_id = (res.get("messages") or [{}])[0].get("id")
            print(
                f"[INSTANT HANDOFF OK] to={from_number!r} pnid={phone_number_id!r} "
                f"sent_id={sent_id!r} reason={reason[:80]!r}"
            )
        except Exception as exc:
            print(f"[INSTANT HANDOFF SEND FAIL] to={from_number!r} error={exc!r}")

    try:
        if sess and not sess.handoff_notified:
            inquiry_dict = sess.inquiry.as_dict() if sess else {}
            ok = await notify.send_handoff_email(
                phone_number=from_number,
                reason=reason,
                partial_inquiry_summary=(
                    "Instant keyword-triggered handoff.\n"
                    f"Inquiry fields so far: {json.dumps({k:v for k,v in inquiry_dict.items() if v})}"
                ),
                relationship_summary=sess.relationship_summary(),
                recent_transcript=sess.recent_transcript(),
            )
            if ok and sess:
                sess.handoff_notified = True
    except Exception as exc:
        print(f"[INSTANT HANDOFF EMAIL FAIL] to={from_number!r} error={exc!r}")

    if sess:
        await conversation_store.save_session(sess)


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

    scanned_from_redis: int = 0
    try:
        scanned_from_redis = await conversation_store.redis_scan_all_sessions()
    except Exception as _scan_exc:
        print(f"[FOLLOWUPS] redis_scan_all_sessions best-effort failed: {_scan_exc!r}")

    pruned = conversation_store.prune_old_sessions()
    actions = conversation_store.scan_for_followups()
    actions = actions[:MAX_NUDGES_PER_RUN]

    ready_actions: list[Any] = []
    deferred_count = 0
    for act in actions:
        ok_now, next_ts = scheduler.compute_next_sendable_for_e164(act.phone_number)
        if ok_now:
            ready_actions.append(act)
            continue
        deferred_count += 1
        try:
            scheduled_for_iso = datetime.datetime.fromtimestamp(
                float(next_ts), tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            scheduled_for_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        row: Dict[str, Any] = {
            "e164": act.phone_number,
            "direction": "ai_nudge",
            "template_name": None,
            "template_params": "{}",
            "plain_text": act.message_text,
            "sender_direction": "ai",
            "created_by": "cron:followups-scan",
            "scheduled_for": scheduled_for_iso,
            "status": "pending",
            "claim_session_id": None,
        }
        async def _deferred_persist(r: Dict[str, Any]) -> None:
            if not _sb_enabled():
                return
            try:
                async with asyncio.timeout(2.0):  # type: ignore[attr-defined]
                    await _sb.insert_outbound_schedule(r)  # type: ignore[attr-defined]
            except Exception as exc:
                print(
                    f"[PERSIST-WARN] outbound_schedule deferred save e164={r.get('e164')!r} "
                    f"failed: {type(exc).__name__}: {str(exc)[:120]}"
                )
        asyncio.create_task(_deferred_persist(row))

    sent = 0
    failed = 0
    skipped_claim = 0
    results: list[dict] = []
    for act in ready_actions:
        # --- claim mutex + 72h idle carve-out (additive, no break) ---
        # If chat is claimed by a human, we NORMALLY skip the nudge (the human
        # is working it). But if the last buyer message was ≥ 72 hours ago,
        # the human almost certainly forgot their tab open (stale claim). In
        # that case we still send the nurture nudge — it's better than letting
        # a 3-day-cold lead go silent forever.
        held = await _claim_is_held_by_other(act.phone_number)
        last_buyer_ts: Optional[float] = None
        try:
            sess = conversation_store._SESSIONS.get(act.phone_number)
            if sess and hasattr(sess, "last_buyer_message_at") and sess.last_buyer_message_at:
                last_buyer_ts = float(sess.last_buyer_message_at)
        except Exception:
            last_buyer_ts = None
        stale_cutoff = time.time() - (72 * 3600)
        claim_blocks = held and last_buyer_ts is not None and last_buyer_ts > stale_cutoff
        if claim_blocks:
            print(
                f"[FOLLOWUPS SKIP CLAIM] phone={act.phone_number!r} kind={act.kind!r} "
                f"held_by={held.get('held_by')!r} expires_in={held.get('expires_in_secs')}s "
                f"last_buyer_ago_h={(time.time() - last_buyer_ts) / 3600:.1f}h"
            )
            skipped_claim += 1
            results.append({
                **act.as_dict(),
                "sent": False,
                "skipped": "claim_held",
                "held_by": held.get("held_by"),
                "expires_in_secs": held.get("expires_in_secs"),
            })
            continue
        if held and last_buyer_ts is None:
            # No message timestamp info — conservative: still skip (stay safe).
            print(
                f"[FOLLOWUPS SKIP CLAIM (no ts)] phone={act.phone_number!r} kind={act.kind!r} "
                f"held_by={held.get('held_by')!r}"
            )
            skipped_claim += 1
            results.append({
                **act.as_dict(),
                "sent": False,
                "skipped": "claim_held_no_timestamp",
            })
            continue

        try:
            res = await send_whatsapp_text(
                to=act.phone_number,
                text=act.message_text,
                preview_url=False,
                sender_direction="ai",
            )
            await conversation_store.mark_followup_sent(act.phone_number, act.kind)
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
        f"actions_total={len(actions)} ready={len(ready_actions)} "
        f"deferred_due_to_quiet_window={deferred_count} "
        f"scanned_from_redis={scanned_from_redis} "
        f"sent={sent} failed={failed} skipped_claim={skipped_claim}"
    )
    return JSONResponse(
        content={
            "pruned": pruned,
            "scanned_sessions": scanned_sessions,
            "actions_returned": len(ready_actions),
            "actions_total": len(actions),
            "deferred_due_to_quiet_window": deferred_count,
            "scanned_from_redis": int(scanned_from_redis or 0),
            "sent": sent,
            "failed": failed,
            "skipped_claim": skipped_claim,
            "actions": results,
        },
        status_code=status.HTTP_200_OK,
    )


@app.post("/scheduled-send-flush")
async def scheduled_send_flush(request: Request) -> JSONResponse:
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
        print("[SCHEDULED-FLUSH] WARN: FOLLOWUPS_CRON_TOKEN env not set — anyone can trigger this.")

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    skipped_claim: int = 0

    if not _sb_enabled():
        return JSONResponse(
            content={"claimed": 0, "sent": 0, "failed": 0, "skipped_claim": 0, "detail": "Supabase not configured"},
            status_code=status.HTTP_200_OK,
        )

    try:
        batch = await _sb.claim_next_pending_scheduled_batch(time.time(), 200)  # type: ignore[attr-defined]
    except Exception as _batch_exc:
        print(f"[SCHEDULED-FLUSH] claim_next_pending_scheduled_batch failed: {_batch_exc!r}")
        return JSONResponse(
            content={"claimed": 0, "sent": 0, "failed": 0, "skipped_claim": 0, "detail": "batch claim error"},
            status_code=status.HTTP_200_OK,
        )

    if not batch:
        return JSONResponse(
            content={"claimed": 0, "sent": 0, "failed": 0, "skipped_claim": 0},
            status_code=status.HTTP_200_OK,
        )

    now_utc = time.time()
    for row in batch:
        claimed += 1
        row_id: Optional[int] = None
        try:
            row_id = int(row.get("id"))  # type: ignore[arg-type]
        except Exception:
            row_id = None
        e164 = row.get("e164") or ""
        direction = row.get("direction") or "ai_nudge"
        plain_text = row.get("plain_text")
        template_name = row.get("template_name")
        template_params_raw = row.get("template_params") or {}
        template_params: Any = template_params_raw
        if isinstance(template_params_raw, str):
            try:
                template_params = json.loads(template_params_raw)
            except Exception:
                template_params = []
        language = row.get("language") or "en_US"
        sender_dir = row.get("sender_direction") or "ai"
        claim_session_id = row.get("claim_session_id")
        row_phone_number_id = row.get("phone_number_id")

        if direction == "human_send":
            held_by_other = await _claim_is_held_by_other(e164)
            if held_by_other and (held_by_other.get("session_id") or "") != (claim_session_id or ""):
                skipped_claim += 1
                if row_id is not None:
                    asyncio.create_task(_sb.update_schedule_status(  # type: ignore[attr-defined]
                        row_id, "failed",
                        error_detail="claim held by other session for human_send",
                    ))
                continue
        else:
            held_by_other = await _claim_is_held_by_other(e164)
            last_buyer_ts: Optional[float] = None
            try:
                sess = conversation_store._SESSIONS.get(e164)
                if sess and hasattr(sess, "last_buyer_message_at") and sess.last_buyer_message_at:
                    last_buyer_ts = float(sess.last_buyer_message_at)
            except Exception:
                last_buyer_ts = None
            stale_cutoff = now_utc - (72 * 3600)
            claim_blocks = held_by_other and last_buyer_ts is not None and last_buyer_ts > stale_cutoff
            if claim_blocks:
                skipped_claim += 1
                if row_id is not None:
                    asyncio.create_task(_sb.update_schedule_status(  # type: ignore[attr-defined]
                        row_id, "failed",
                        error_detail="claim held by other (non-stale)",
                    ))
                continue
            if held_by_other and last_buyer_ts is None:
                skipped_claim += 1
                if row_id is not None:
                    asyncio.create_task(_sb.update_schedule_status(  # type: ignore[attr-defined]
                        row_id, "failed",
                        error_detail="claim held by other (no ts)",
                    ))
                continue

        send_ok = False
        send_exc: Optional[Exception] = None
        try:
            if direction in ("ai_nudge", "human_send") and plain_text:
                await send_whatsapp_text(
                    to=e164,
                    text=str(plain_text),
                    sender_direction=sender_dir,
                    phone_number_id=row_phone_number_id,
                )
                send_ok = True
            elif direction == "template_send" and template_name:
                await send_whatsapp_template(
                    e164,
                    str(template_name),
                    language_code=str(language),
                    params=template_params,
                    sender_direction=sender_dir,
                    phone_number_id=row_phone_number_id,
                )
                send_ok = True
            else:
                send_exc = RuntimeError(f"unsupported schedule row: direction={direction!r} template={template_name!r} text={bool(plain_text)}")
        except Exception as exc:
            send_exc = exc

        if row_id is not None:
            if send_ok:
                sent += 1
                asyncio.create_task(_sb.update_schedule_status(  # type: ignore[attr-defined]
                    row_id, "sent", sent_at_utc=time.time(),
                ))
            else:
                failed += 1
                err = repr(send_exc)[:480] if send_exc else "unknown"
                asyncio.create_task(_sb.update_schedule_status(  # type: ignore[attr-defined]
                    row_id, "failed", error_detail=err,
                ))
        else:
            if send_ok:
                sent += 1
            else:
                failed += 1

    print(
        f"[SCHEDULED-FLUSH] claimed={claimed} sent={sent} failed={failed} "
        f"skipped_claim={skipped_claim}"
    )
    return JSONResponse(
        content={
            "claimed": claimed,
            "sent": sent,
            "failed": failed,
            "skipped_claim": skipped_claim,
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


# ============================================================================
# Static files mount for /inbox/* page assets (CSS/JS).
# Uses Jinja2 templates reference /inbox/static/app.css + /inbox/static/app.js.
# FastAPI StaticFiles is mounted lazily (on app startup) — safe if folder missing → 404.
# ============================================================================
try:
    from fastapi.staticfiles import StaticFiles as _StaticFiles  # type: ignore
    import os as _static_os
    _static_dir = _static_os.path.join(_static_os.path.dirname(_static_os.path.abspath(__file__)), "petrobind_frontend_app", "static")
    # Fix: dirname() returns a tuple because of stray comma above — fix properly below
except Exception:
    _StaticFiles = None  # type: ignore
try:
    import os as _static_os2
    _static_dir = _static_os2.path.join(
        _static_os2.path.dirname(_static_os2.path.abspath(__file__)),
        "petrobind_frontend_app",
        "static",
    )
    if _static_os2.path.isdir(_static_dir) and _StaticFiles is not None:
        app.mount("/inbox/static", _StaticFiles(directory=_static_dir, html=False), name="inbox_static")
        app.mount("/static", _StaticFiles(directory=_static_dir, html=False), name="static_root")
except Exception as _static_exc:  # pragma: no cover — best-effort mount
    print(f"[INBOX] static mount skipped: {type(_static_exc).__name__}: {_static_exc!s}")


# ============================================================================
# Shared Inbox HTML pages (/inbox/*) — Jinja2 + signed cookie session auth
# ============================================================================

def _issue_inbox_session(*, held_by_name: str) -> tuple[str, str]:
    """Create a new inbox admin session. Returns (session_id: str, signed_cookie_value: str).

    Session payload: {sid, sub="admin", name, iat}
    Signed with itsdangerous URLSafeTimedSerializer so the user can't tamper.
    Expiry is verified server-side by max_age at read time, not baked into cookie.
    """
    sid = _secrets.token_urlsafe(24) if _secrets else f"sid-{int(time.time()*1000)}"
    payload = {
        "sid": sid,
        "sub": "admin",
        "name": held_by_name or INBOX_ADMIN_NAME,
        "iat": int(time.time()),
    }
    signed = sid  # fallback if itsdangerous missing → unsigned, risk but usable
    if _ITS_DANGEROUS_OK and _URL_SAFE_SERIALIZER is not None:
        signed = _URL_SAFE_SERIALIZER.dumps(payload)
    else:
        import json as _json_for_cookie
        signed = _base64.urlsafe_b64encode(_json_for_cookie.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return sid, signed


def _verify_inbox_session_cookie(request: Request) -> Optional[dict]:
    """Parse + validate the inbox_session cookie. Returns payload dict on success, None on failure.

    Verifies: cookie present, signature OK signature (itsdangerous max_age = INBOX_SESSION_EXPIRE_MINUTES*60, sub == "admin".
    """
    raw = request.cookies.get("inbox_session")
    if not raw:
        return None
    payload: Optional[dict] = None
    if _ITS_DANGEROUS_OK and _URL_SAFE_SERIALIZER is not None:
        try:
            max_age = INBOX_SESSION_EXPIRE_MINUTES * 60
            payload = _URL_SAFE_SERIALIZER.loads(raw, max_age=max_age)
        except Exception:
            return None
    else:
        # Fallback: parse the base64 payload (no signature verification — only when itsdangerous broken in dev)
        try:
            import json as _json_for_cookie
            padding = "=" * (-len(raw) % 4)
            data = _base64.urlsafe_b64decode((raw + padding).encode("ascii"))
            payload = _json_for_cookie.loads(data.decode("utf-8"))
        except Exception:
            return None
    if not isinstance(payload, dict) or payload.get("sub") != "admin":
        return None
    if "sid" not in payload:
        return None
    return payload


def _inbox_page_gating_checks(request: Request) -> Optional[Response]:
    """Shared pre-flight check for /inbox/* HTML routes. Returns None if OK,
    else a Response (503/redirect) if not ready/not logged in.

    Supabase is OPTIONAL (additive failure-isolated layer). We do NOT block
    here on _sb_enabled() because chat_list / chat_thread templates +
    supabase_client.py methods all degrade to empty [] / None / True safely
    when Supabase URL/KEY are blank.  Only INBOX_ADMIN_TOKEN and Jinja2
    template env are mandatory hard gates.
    """
    if not INBOX_ADMIN_TOKEN:
        body = ("<html><body><h1>503 — Inbox Login Not Configured</h1>" \
               "<p>INBOX_ADMIN_TOKEN env var is not set. Generate one with " \
               "<code>python3 -c 'import secrets; print(secrets.token_urlsafe(32))</code>, " \
               "add it to env, restart.</p></body></html>")
        return HTMLResponse(content=body, status_code=503)
    if _JINJA_ENV is None:
        body = (
            "<html><body><h1>503 — Templates Missing</h1>"
            f"<p>Jinja2 setup error: {_JINJA_TEMPLATE_ERROR or 'unknown'}</p>"
            "</body></html>"
        )
        return HTMLResponse(content=body, status_code=503)
    return None


def _render_template(template_name: str, **ctx) -> HTMLResponse:
    """Render a Jinja2 template to HTMLResponse. 500 on render failure."""
    assert _JINJA_ENV is not None  # gated upstream
    try:
        tpl = _JINJA_ENV.get_template(template_name)
        html = tpl.render(**ctx)
        return HTMLResponse(content=html)
    except Exception as exc:
        body = (
            "<html><body>"
            f"<h1>500 — Template Render Error</h1>"
            f"<p>Template: {template_name!r}</p>"
            f"<pre>{type(exc).__name__}: {exc!s}</pre>"
            "</body></html>"
        )
        return HTMLResponse(content=body, status_code=500)


def _request_is_https(request: Request) -> bool:
    """Heuristic: True if the client came over HTTPS. Sets Secure flag on cookie iff True."""
    # 1) request.url.scheme (honest if Render proxy sets X-Forwarded-Proto correctly)
    if request.url.scheme == "https":
        return True
    # 2) X-Forwarded-Proto header set by Render / reverse proxy
    xf_proto = request.headers.get("x-forwarded-proto") or request.headers.get("X-Forwarded-Proto")
    if xf_proto and xf_proto.lower() == "https":
        return True
    # 3) .onrender.com host is always HTTPS on Render
    host = (request.headers.get("host") or request.url.hostname or "").lower()
    if host.endswith(".onrender.com"):
        return True
    return False


@app.get("/inbox/login")
async def inbox_login_page(request: Request) -> Response:
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate
    # Already logged in? → skip straight to /inbox/chats
    sess = _verify_inbox_session_cookie(request)
    if sess:
        return RedirectResponse(url="/inbox/chats", status_code=302)
    error = request.query_params.get("error")
    return _render_template(
        "login.html",
        error=error,
        admin_name=INBOX_ADMIN_NAME,
    )


@app.post("/inbox/login")
async def inbox_login_submit(request: Request) -> Response:
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate
    # Read form body (password = the INBOX_ADMIN_TOKEN)
    form_data = None
    try:
        form_data = await request.form()
    except Exception:
        form_data = {}
    password = ""
    if form_data is not None:
        password = (form_data.get("password") or "").strip()
    if not password or password != INBOX_ADMIN_TOKEN:
        # Bad password → redirect back to login with error flash
        return RedirectResponse(url="/inbox/login?error=bad-password", status_code=302)
    # Good password → issue signed session cookie
    session_id, signed_cookie = _issue_inbox_session(held_by_name=INBOX_ADMIN_NAME)
    secure_flag = _request_is_https(request)
    cookie_attrs = {
        "key": "inbox_session",
        "value": signed_cookie,
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": secure_flag,
        "max_age": INBOX_SESSION_EXPIRE_MINUTES * 60,
    }
    # Use RedirectResponse with set_cookie
    resp = RedirectResponse(url="/inbox/chats", status_code=302)
    resp.set_cookie(**cookie_attrs)
    # Also set a readable (non-HttpOnly) JS-readable cookie with just the session_id,
    # so frontend app.js can pass it as X-Inbox-Session-Id header on claim/send REST calls.
    resp.set_cookie(
        key="inbox_sid",
        value=session_id,
        path="/",
        httponly=False,  # JS needs this one
        samesite="lax",
        secure=secure_flag,
        max_age=INBOX_SESSION_EXPIRE_MINUTES * 60,
    )
    asyncio.create_task(_sb.audit(
        actor=INBOX_ADMIN_NAME,
        action="login",
        detail={"session_id": session_id},
    ))
    return resp


@app.get("/inbox/logout")
async def inbox_logout(request: Request) -> Response:
    resp = RedirectResponse(url="/inbox/login", status_code=302)
    resp.delete_cookie(key="inbox_session", path="/")
    resp.delete_cookie(key="inbox_sid", path="/")
    return resp


@app.get("/inbox")
async def inbox_root(request: Request) -> Response:
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate
    sess = _verify_inbox_session_cookie(request)
    if not sess:
        return RedirectResponse(url="/inbox/login", status_code=302)
    return RedirectResponse(url="/inbox/chats", status_code=302)


# ---------------------------------------------------------------------------
# Read-only history fallback: when Supabase is not configured (e.g. local dev),
# show past conversations straight from the Redis/in-process sessions so the
# inbox is never blank.  Never used when Supabase is enabled.
# ---------------------------------------------------------------------------
def _iso_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts or 0))


def _unique_by_number(chats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per phone number ("+60123" and "60123" are the same contact);
    input must already be newest-first, so the first row per number wins."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for c in chats:
        key = contact_names.normalize(c.get("e164") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


async def _attach_names(chats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add `display_name` (human-set label, '' if none) to every chat row."""
    names = await contact_names.get_all()
    for c in chats:
        c["display_name"] = names.get(contact_names.normalize(c.get("e164") or ""), "")
    return chats


async def _history_chats() -> List[Dict[str, Any]]:
    try:
        await conversation_store.redis_scan_all_sessions()
    except Exception as exc:
        print(f"[INBOX] history redis scan failed: {type(exc).__name__}: {exc!s}")
    rows: List[Dict[str, Any]] = []
    for sess in conversation_store.all_sessions():
        turns = [m for m in sess.history if m.get("role") in ("user", "assistant") and m.get("content")]
        if not turns:
            continue
        last = turns[-1]
        try:
            inq = asdict(sess.inquiry)
        except Exception:
            inq = {}
        rows.append({
            "e164": sess.phone_number,
            "inquiry_jsonb": inq,
            "lead_notified": sess.lead_notified,
            "handoff_notified": sess.handoff_notified,
            "last_message_at": _iso_ts(sess.last_activity_ts),
            "last_message_is_buyer": last["role"] == "user",
            "last_direction": "buyer" if last["role"] == "user" else "ai",
            "last_message_text": str(last["content"])[:200],
            "last_message_created_at": _iso_ts(sess.last_activity_ts),
            "claim_held_by": None,
            "claim_expires_at": None,
        })
    rows.sort(key=lambda r: r["last_message_at"], reverse=True)
    return _unique_by_number(rows)


async def _history_thread(e164: str) -> List[Dict[str, Any]]:
    sess = None
    for cand in conversation_store.all_sessions():
        if cand.phone_number == e164:
            sess = cand
            break
    if sess is None:
        try:
            await conversation_store.redis_scan_all_sessions()
        except Exception:
            pass
        for cand in conversation_store.all_sessions():
            if cand.phone_number == e164:
                sess = cand
                break
    if sess is None:
        return []
    created = _iso_ts(sess.last_activity_ts)
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(sess.history):
        if m.get("role") not in ("user", "assistant") or not m.get("content"):
            continue
        out.append({
            "id": f"hist-{i}",
            "wamid": "",
            "direction": "buyer" if m["role"] == "user" else "ai",
            "e164": e164,
            "text": str(m["content"]),
            "created_at": created,
            "errored": False,
        })
    return out


@app.get("/inbox/chats")
async def inbox_chat_list(request: Request) -> Response:
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate
    sess = _verify_inbox_session_cookie(request)
    if not sess:
        return RedirectResponse(url="/inbox/login", status_code=302)
    admin_name = sess.get("name") or INBOX_ADMIN_NAME
    session_id = sess.get("sid") or ""
    try:
        chats = await _sb.list_chats(limit=200)
    except Exception as exc:
        print(f"[INBOX] chat_list DB error: {type(exc).__name__}: {exc!s}")
        chats = []
    if not chats and not _sb.ENABLED:
        chats = await _history_chats()
    chats = await _attach_names(_unique_by_number(chats))
    # Inject convenience flags the template needs
    return _render_template(
        "chat_list.html",
        chats=chats,
        admin_name=admin_name,
        session_id=session_id,
        inbox_admin_token_preview=(
            INBOX_ADMIN_TOKEN[:6] + "…" + INBOX_ADMIN_TOKEN[-4:] if len(INBOX_ADMIN_TOKEN) > 12 else "****"
        ),
        supa_url=(os.getenv("SUPABASE_URL") or ""),
    )


@app.get("/inbox/chat/{e164}")
async def inbox_chat_thread(e164: str, request: Request) -> Response:
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate
    sess_payload = _verify_inbox_session_cookie(request)
    if not sess_payload:
        return RedirectResponse(url="/inbox/login", status_code=302)
    admin_name = sess_payload.get("name") or INBOX_ADMIN_NAME
    session_id = sess_payload.get("sid") or ""
    try:
        msgs = await _sb.thread_messages(e164, limit=300)
    except Exception as exc:
        print(f"[INBOX] chat_thread DB error {e164!r}: {type(exc).__name__}: {exc!s}")
        msgs = []
    if not msgs and not _sb.ENABLED:
        msgs = await _history_thread(e164)
    contact_name = (await contact_names.get_all()).get(contact_names.normalize(e164), "")
    last_buyer_wamid = ""
    for m in reversed(msgs):
        if m.get("direction") == "buyer":
            last_buyer_wamid = m.get("wamid") or ""
            break
    return _render_template(
        "chat_thread.html",
        e164=e164,
        embed=(request.query_params.get("embed") == "1"),
        contact_name=contact_name,
        messages=msgs,
        admin_name=admin_name,
        session_id=session_id,
        last_buyer_wamid=last_buyer_wamid,
        supa_url=(os.getenv("SUPABASE_URL") or ""),
        supa_anon_key=(os.getenv("SUPABASE_ANON_KEY") or ""),
    )


# ============================================================================
# Shared Inbox REST API (/api/inbox/*)
#
# All routes are:
#   1. PRE-GATED 503 if Supabase inbox env vars are not configured (_sb_enabled=False)
#   2. PRE-GATED 401 if Bearer token != INBOX_ADMIN_TOKEN
#
# Chat list, message thread, human-send, claim acquire/release, AI suggestion pill.
# ============================================================================

# LRU (by age) in-memory cache for AI suggestion pills. Key = (e164, last_buyer_wamid),
# value = (monotonic_cache_time_seconds, suggestion_text: str). TTL 300s so a UI
# refresh doesn't burn duplicate LLM tokens — but we don't cache forever so new
# context still triggers fresh suggestions.
_SUGGESTION_CACHE: Dict[Tuple[str, str], Tuple[float, str]] = {}
_SUGGESTION_CACHE_TTL_SEC = 300


def _evict_stale_suggestions() -> None:
    """Drop cache entries older than TTL. Runs on every GET suggestion request."""
    now = time.monotonic()
    stale_keys = [
        k for k, (ts, _) in _SUGGESTION_CACHE.items()
        if now - ts > _SUGGESTION_CACHE_TTL_SEC
    ]
    for k in stale_keys:
        _SUGGESTION_CACHE.pop(k, None)


def _requires_inbox_bearer(request: Request) -> Optional[JSONResponse]:
    """Common gating for every /api/inbox/* route. Returns None if OK, else a
    ready-to-return JSONResponse (503 disabled / 401 bad token).

    Supabase is OPTIONAL (additive failure-isolated layer). We do NOT block
    here on _sb_enabled() because supabase_client.py methods degrade to
    empty [] / None / True safely when ENABLED=False.  Dashboard panels
    render empty data rather than 503 hard block.

    Auth accepted via 2 methods (either OK):
      (1) HTTP "Authorization: Bearer <INBOX_ADMIN_TOKEN>" header (used by
          admin.js sessionStorage bearer prompt), OR
      (2) Valid signed inbox_session cookie (issued by /inbox/login form).
    """
    if not INBOX_ADMIN_TOKEN:
        return JSONResponse(
            content={"detail": "INBOX_ADMIN_TOKEN not configured on server"},
            status_code=503,
        )
    via_header = _bearer_token(request)
    header_ok = bool(via_header and via_header == INBOX_ADMIN_TOKEN)
    cookie_ok = _verify_inbox_session_cookie(request) is not None
    if not (header_ok or cookie_ok):
        return JSONResponse(
            content={"detail": "Unauthorized: need Bearer INBOX_ADMIN_TOKEN header OR valid signed inbox_session cookie"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return None


@app.get("/api/inbox/chats")
async def api_inbox_list_chats(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    limit = 200
    try:
        lq = request.query_params.get("limit")
        if lq and lq.isdigit():
            limit = max(1, min(1000, int(lq)))
    except Exception:
        pass
    try:
        chats = await _sb.list_chats(limit=limit)
        if not chats and not _sb.ENABLED:
            chats = await _history_chats()
        chats = await _attach_names(_unique_by_number(chats))
    except Exception as exc:
        print(f"[INBOX] list_chats error: {type(exc).__name__}: {exc!s}")
        return JSONResponse(
            content={"detail": f"Database error: {type(exc).__name__}"},
            status_code=502,
        )
    return JSONResponse(content={"chats": chats})


@app.get("/api/inbox/chats/{e164}/messages")
async def api_inbox_thread_messages(e164: str, request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    limit = 200
    try:
        lq = request.query_params.get("limit")
        if lq and lq.isdigit():
            limit = max(1, min(1000, int(lq)))
    except Exception:
        pass
    try:
        msgs = await _sb.thread_messages(e164, limit=limit)
        if not msgs and not _sb.ENABLED:
            msgs = await _history_thread(e164)
    except Exception as exc:
        print(f"[INBOX] thread_messages {e164!r} error: {type(exc).__name__}: {exc!s}")
        return JSONResponse(
            content={"detail": f"Database error: {type(exc).__name__}"},
            status_code=502,
        )
    return JSONResponse(content={"e164": e164, "messages": msgs})


@app.post("/api/inbox/chats/{e164}/messages")
async def api_inbox_send_human_message(e164: str, request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="'text' (non-empty string) required")
    reply_to = body.get("reply_to_wamid")
    if reply_to is not None:
        reply_to = str(reply_to) or None

    # --- claim check: only the holder (or unclaimed) can send ---
    # We extract the session_id from a custom header if present (the frontend
    # sets it on each request after login), else treat as generic admin send.
    my_session_id = (
        request.headers.get("X-Inbox-Session-Id")
        or f"rest-send-{secrets.token_hex(6)}"
    )
    held = await _sb.claim_is_human_held(e164)
    if held and held.get("session_id") != my_session_id:
        # Someone else holds the claim → 409
        return JSONResponse(
            content={
                "success": False,
                "detail": (
                    f"Chat claimed by {held.get('held_by') or 'another admin'}; "
                    f"release claim or wait for expiry ({held.get('expires_in_secs')}s)"
                ),
                "held_by": held.get("held_by"),
                "expires_in_secs": held.get("expires_in_secs"),
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    # Optional: pre-acquire claim with my_session_id for 120s if unclaimed
    # so the AI pipeline definitely won't race us in the next ~2 min.
    if not held:
        try:
            await _sb.claim_acquire(
                e164=e164,
                held_by=INBOX_ADMIN_NAME,
                session_id=my_session_id,
                ttl_seconds=120,
            )
        except Exception as exc:
            print(f"[INBOX] pre-send claim acquire best-effort failed: {exc!r}")

    try:
        result = await send_whatsapp_text(
            to=e164,
            text=text,
            reply_to_message_id=reply_to,
            sender_direction="human",
        )
    except RuntimeError as exc:
        return JSONResponse(
            content={"success": False, "error": str(exc)},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    sent_id = (result.get("messages") or [{}])[0].get("id") if result else None
    await _clear_needs_human(e164)
    asyncio.create_task(_sb.audit(
        actor=INBOX_ADMIN_NAME,
        action="human_send",
        e164=e164,
        detail={"session_id": my_session_id, "chars": len(text), "sent_id": sent_id},
    ))
    return JSONResponse(content={"success": True, "sent_id": sent_id, "result": result})


@app.post("/api/inbox/chats/{e164}/claim")
async def api_inbox_acquire_claim(e164: str, request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    # session_id: prefer X-Inbox-Session-Id header (frontend), else generate
    my_session_id = (
        request.headers.get("X-Inbox-Session-Id")
        or request.headers.get("x-inbox-session-id")
        or secrets.token_urlsafe(24)
    )
    ttl = 120
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("ttl_seconds"):
            ttl = max(10, min(3600, int(body["ttl_seconds"])))
    except Exception:
        pass
    try:
        acquired, conflict = await _sb.claim_acquire(
            e164=e164,
            held_by=INBOX_ADMIN_NAME,
            session_id=my_session_id,
            ttl_seconds=ttl,
        )
    except Exception as exc:
        print(f"[INBOX] claim_acquire error {e164!r}: {type(exc).__name__}: {exc!s}")
        return JSONResponse(
            content={"detail": f"Database error: {type(exc).__name__}"},
            status_code=502,
        )
    if acquired:
        asyncio.create_task(_sb.audit(
            actor=INBOX_ADMIN_NAME,
            action="claim_acquire",
            e164=e164,
            detail={"session_id": my_session_id, "ttl_seconds": ttl},
        ))
        return JSONResponse(content={
            "acquired": True,
            "expires_in_secs": ttl,
            "session_id": my_session_id,
        })
    # 409 conflict
    asyncio.create_task(_sb.audit(
        actor=INBOX_ADMIN_NAME,
        action="claim_conflict",
        e164=e164,
        detail={"session_id": my_session_id, "conflict_with": conflict},
    ))
    return JSONResponse(
        content={
            "acquired": False,
            "held_by": (conflict or {}).get("held_by"),
            "expires_in_secs": (conflict or {}).get("expires_in_secs"),
            "my_session_id": my_session_id,
        },
        status_code=status.HTTP_409_CONFLICT,
    )


@app.get("/api/inbox/chats/{e164}/claim")
async def api_inbox_claim_status(e164: str, request: Request) -> JSONResponse:
    """Who is replying to this chat right now? (human mode = a live claim)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    my_sid = request.headers.get("X-Inbox-Session-Id") or request.headers.get("x-inbox-session-id") or ""
    try:
        held = await _sb.claim_is_human_held(e164)
    except Exception as exc:
        print(f"[INBOX] claim_status error {e164!r}: {type(exc).__name__}: {exc!s}")
        held = None
    if not held:
        return JSONResponse(content={"held": False, "mine": False})
    return JSONResponse(content={
        "held": True,
        "mine": bool(my_sid and held.get("session_id") == my_sid),
        "held_by": held.get("held_by"),
        "expires_in_secs": held.get("expires_in_secs"),
    })


@app.delete("/api/inbox/chats/{e164}/claim")
async def api_inbox_release_claim(e164: str, request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    my_session_id = (
        request.headers.get("X-Inbox-Session-Id")
        or request.headers.get("x-inbox-session-id")
    )
    try:
        ok = await _sb.claim_release(
            e164=e164,
            held_by=INBOX_ADMIN_NAME,
            session_id=my_session_id,
        )
    except Exception as exc:
        print(f"[INBOX] claim_release error {e164!r}: {type(exc).__name__}: {exc!s}")
        return JSONResponse(
            content={"detail": f"Database error: {type(exc).__name__}"},
            status_code=502,
        )
    asyncio.create_task(_sb.audit(
        actor=INBOX_ADMIN_NAME,
        action="claim_release",
        e164=e164,
        detail={"session_id": my_session_id, "released": ok},
    ))
    return JSONResponse(content={"ok": True, "released": ok})


class _NameBody(BaseModel):
    name: str = ""


@app.put("/api/inbox/chats/{e164}/name")
async def api_inbox_set_contact_name(e164: str, body: _NameBody, request: Request) -> JSONResponse:
    """Set (or clear, with an empty string) the reference name for a number."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        stored = await contact_names.set_name(e164, body.name)
    except ValueError:
        return JSONResponse(content={"detail": "invalid number"}, status_code=400)
    except Exception as exc:
        print(f"[INBOX] set_contact_name error: {type(exc).__name__}: {exc!s}")
        return JSONResponse(content={"detail": "could not save name"}, status_code=502)
    return JSONResponse(content={"ok": True, "e164": e164, "name": stored})


# ---------------------------------------------------------------------------
# Human-handoff queue: chats the AI escalated, with the WhatsApp 24h reply
# window countdown (runs from the buyer's last message).
# ---------------------------------------------------------------------------
WINDOW_SECONDS = 86400


def _find_session_by_number(e164: str):
    key = contact_names.normalize(e164)
    for cand in conversation_store.all_sessions():
        if contact_names.normalize(cand.phone_number) == key:
            return cand
    return None


async def _clear_needs_human(e164: str) -> bool:
    """Mark a chat as handled by a human. Returns True if it was pending."""
    sess = _find_session_by_number(e164)
    if sess is None or not sess.needs_human_since:
        return False
    sess.needs_human_since = None
    sess.needs_human_reason = ""
    try:
        await conversation_store.save_session(sess)
    except Exception as exc:
        print(f"[INBOX] clear needs_human save failed: {type(exc).__name__}: {exc!s}")
    return True


@app.get("/api/inbox/handoffs")
async def api_inbox_handoffs(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        await conversation_store.redis_scan_all_sessions()
    except Exception as exc:
        print(f"[INBOX] handoffs redis scan failed: {type(exc).__name__}: {exc!s}")
    names = await contact_names.get_all()
    now = time.time()
    items: List[Dict[str, Any]] = []
    for sess in conversation_store.all_sessions():
        if not sess.needs_human_since:
            continue
        last_buyer = sess.last_buyer_ts or sess.last_activity_ts
        turns = [m for m in sess.history if m.get("role") in ("user", "assistant") and m.get("content")]
        last = turns[-1] if turns else {}
        closes = float(last_buyer) + WINDOW_SECONDS
        items.append({
            "e164": sess.phone_number,
            "display_name": names.get(contact_names.normalize(sess.phone_number), ""),
            "reason": sess.needs_human_reason,
            "since": sess.needs_human_since,
            "last_buyer_ts": last_buyer,
            "window_closes_at": closes,
            "seconds_left": int(closes - now),
            "last_message_text": str(last.get("content", ""))[:160],
            "last_message_from": "buyer" if last.get("role") == "user" else "ai",
        })
    items.sort(key=lambda i: i["window_closes_at"])
    return JSONResponse(content={"now": now, "handoffs": items})


@app.post("/api/inbox/chats/{e164}/handoff/resolve")
async def api_inbox_handoff_resolve(e164: str, request: Request) -> JSONResponse:
    """Dismiss a handoff without replying (e.g. handled on the phone)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    cleared = await _clear_needs_human(e164)
    return JSONResponse(content={"ok": True, "cleared": cleared})


# ---------------------------------------------------------------------------
# Admin: delete a chat (with transcript kept in the logs) / import old history
# ---------------------------------------------------------------------------
@app.delete("/api/inbox/chats/{e164}")
async def api_inbox_delete_chat(e164: str, request: Request) -> JSONResponse:
    """Permanently delete a conversation (Supabase + Redis + name). A transcript copy is written to the logs."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    key = contact_names.normalize(e164)
    if not key:
        return JSONResponse(content={"detail": "invalid number"}, status_code=400)
    try:
        await conversation_store.redis_scan_all_sessions()
    except Exception:
        pass
    sess = _find_session_by_number(e164)
    variants = {e164, key, "+" + key}
    if sess is not None:
        variants.add(sess.phone_number)
    msgs: List[Dict[str, Any]] = []
    try:
        msgs = await _sb.thread_messages(e164, limit=300)
    except Exception:
        msgs = []
    if not msgs:
        msgs = await _history_thread(sess.phone_number if sess is not None else e164)
    names = await contact_names.get_all()
    name = names.get(key, "")
    transcript = [
        {"direction": m.get("direction"), "text": str(m.get("text") or "")[:1000], "at": m.get("created_at")}
        for m in msgs[-200:]
    ]
    removed = await _sb.delete_chat(sorted(variants))
    for v in variants:
        await conversation_store.reset_session(v)
    try:
        await contact_names.set_name(e164, "")
    except Exception:
        pass
    await _sb.audit(
        actor=INBOX_ADMIN_NAME,
        action="chat_delete",
        e164=e164,
        detail={
            "contact_name": name,
            "messages_in_transcript": len(transcript),
            "supabase_removed": removed,
            "redis_session_removed": sess is not None,
            "transcript": transcript,
        },
    )
    return JSONResponse(content={"ok": True, "e164": e164, "supabase_removed": removed, "logged": True})


def _iso_utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@app.post("/api/inbox/admin/import-history")
async def api_inbox_import_history(request: Request) -> JSONResponse:
    """Copy every conversation held in Redis into Supabase (skips chats Supabase already has)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    if not _sb.ENABLED:
        return JSONResponse(content={"detail": "Supabase is not configured on this server"}, status_code=503)
    try:
        await conversation_store.redis_scan_all_sessions(force=True)
    except Exception as exc:
        print(f"[INBOX] import redis scan failed: {type(exc).__name__}: {exc!s}")
    summary = {"chats_imported": 0, "messages_imported": 0, "skipped_already_in_supabase": 0, "skipped_empty": 0, "failed": 0}
    now = time.time()
    for sess in list(conversation_store.all_sessions()):
        turns = [(m["role"], str(m["content"])) for m in sess.history if m.get("role") in ("user", "assistant") and m.get("content")]
        if not turns:
            summary["skipped_empty"] += 1
            continue
        if await _sb.count_messages(sess.phone_number) > 0:
            summary["skipped_already_in_supabase"] += 1
            continue
        t0 = sess.first_seen_ts or sess.last_activity_ts
        t1 = max(sess.last_activity_ts, t0)
        n = len(turns)
        rows = []
        for i, (role, text) in enumerate(turns):
            ts = min(now, t0 + max((t1 - t0) * i / max(n - 1, 1), i))
            rows.append({
                "direction": "buyer" if role == "user" else "ai",
                "e164": sess.phone_number,
                "text": text,
                "created_at": _iso_utc(ts),
                "payload_jsonb": {"imported_from": "redis"},
            })
        sent = await _sb.import_messages(sess.phone_number, rows)
        if not sent:
            summary["failed"] += 1
            continue
        await _sb.mirror_session(
            e164=sess.phone_number,
            inquiry_dict=asdict(sess.inquiry),
            history_list=list(sess.history[-200:]),
            is_new_prospect=sess.inquiry.is_new_prospect,
            lead_notified=bool(sess.lead_notified),
            handoff_notified=bool(sess.handoff_notified),
            booking_intent_notified=bool(sess.booking_intent_notified),
            booking_link_shared_at_ts=sess.booking_link_shared_at,
            followup_nudge_1_ts=sess.booking_followup_sent_at,
            followup_nudge_2_ts=sess.inquiry_followup_sent_at,
        )
        summary["chats_imported"] += 1
        summary["messages_imported"] += sent
    await _sb.audit(actor=INBOX_ADMIN_NAME, action="history_import", detail=summary)
    return JSONResponse(content={"ok": True, **summary})


@app.get("/api/inbox/logs")
async def api_inbox_logs(request: Request) -> JSONResponse:
    """Audit log: logins, claims, human sends, deletions, imports (newest first)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    qp = request.query_params
    limit = int(qp.get("limit", "200")) if (qp.get("limit") or "200").isdigit() else 200
    events = await _sb.list_audit(limit=limit, action=qp.get("action") or None, q=qp.get("q") or None)
    return JSONResponse(content={"source": "supabase" if _sb.ENABLED else "local-file", "events": events})


def _logged_in_page_ctx(request: Request):
    """(redirect_or_gate, ctx) for simple inbox pages."""
    gate = _inbox_page_gating_checks(request)
    if gate:
        return gate, None
    sess = _verify_inbox_session_cookie(request)
    if not sess:
        return RedirectResponse(url="/inbox/login?next=" + request.url.path, status_code=302), None
    return None, {"admin_name": sess.get("name") or INBOX_ADMIN_NAME, "session_id": sess.get("sid") or "", "supa_url": ""}


@app.get("/inbox/logs")
async def web_inbox_logs(request: Request) -> Response:
    resp, ctx = _logged_in_page_ctx(request)
    if resp:
        return resp
    return _render_template("logs.html", **ctx)


@app.get("/inbox/guide")
async def web_inbox_guide(request: Request) -> Response:
    resp, ctx = _logged_in_page_ctx(request)
    if resp:
        return resp
    endpoints = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if not (path.startswith("/api/inbox") or path.startswith("/inbox") or path in ("/health", "/webhook", "/followups-scan")):
            continue
        if path.startswith("/inbox/static"):
            continue
        doc = ((getattr(r.endpoint, "__doc__", "") or "").strip().split("\n")[0]) if hasattr(r, "endpoint") else ""
        for m in sorted(getattr(r, "methods", None) or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            endpoints.append({"method": m, "path": path, "doc": doc})
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return _render_template("guide.html", endpoints=endpoints, **ctx)


# ---------------------------------------------------------------------------
# Reply feedback + learning loop (ratings -> lessons -> approved guidance)
# ---------------------------------------------------------------------------
@app.post("/api/inbox/feedback")
async def api_inbox_add_feedback(request: Request) -> JSONResponse:
    """Rate an AI reply (up/down) with optional tags, note and a better reply."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    try:
        item = await feedback_store.add_feedback(
            e164=str(body.get("e164") or ""),
            ai_text=str(body.get("ai_text") or ""),
            buyer_text=str(body.get("buyer_text") or ""),
            rating=str(body.get("rating") or ""),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else [],
            note=str(body.get("note") or ""),
            better_reply=str(body.get("better_reply") or ""),
            actor=INBOX_ADMIN_NAME,
        )
    except ValueError as exc:
        return JSONResponse(content={"detail": str(exc)}, status_code=422)
    except Exception as exc:
        print(f"[INBOX] add_feedback failed: {type(exc).__name__}: {exc!s}")
        return JSONResponse(content={"detail": "could not save feedback"}, status_code=502)
    learning.invalidate_cache()
    asyncio.create_task(_sb.audit(actor=INBOX_ADMIN_NAME, action="ai_feedback", e164=item["e164"],
                                  detail={"rating": item["rating"], "tags": item["tags"], "has_better_reply": bool(item["better_reply"])}))
    st = await learning.stats()
    if st["unprocessed"] >= learning.AUTO_LEARN_AFTER:
        asyncio.create_task(learning.distill())  # creates *pending* lessons only; a human approves them
    return JSONResponse(content={"ok": True, "feedback": item})


@app.get("/api/inbox/feedback")
async def api_inbox_list_feedback(request: Request) -> JSONResponse:
    """Ratings given so far (optionally for one chat with ?e164=)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    items = await feedback_store.list_feedback(request.query_params.get("e164") or None)
    return JSONResponse(content={"feedback": items[:500]})


@app.get("/api/inbox/learning")
async def api_inbox_learning(request: Request) -> JSONResponse:
    """Learning dashboard data: accuracy trend, lessons, recent feedback."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    return JSONResponse(content={
        "stats": await learning.stats(),
        "lessons": await feedback_store.list_lessons(),
        "recent_feedback": (await feedback_store.list_feedback())[:50],
    })


@app.post("/api/inbox/learning/learn")
async def api_inbox_learning_learn(request: Request) -> JSONResponse:
    """Turn new feedback into pending lessons now."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    return JSONResponse(content=await learning.distill())


@app.patch("/api/inbox/learning/lessons/{lesson_id}")
async def api_inbox_lesson_status(lesson_id: str, request: Request) -> JSONResponse:
    """Approve (active), disable or re-open (pending) a lesson. Approved lessons guide every reply."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        body: Dict[str, Any] = await request.json()
        item = await feedback_store.set_lesson_status(lesson_id, str(body.get("status") or ""))
    except ValueError:
        return JSONResponse(content={"detail": "status must be pending, active or disabled"}, status_code=422)
    except Exception:
        return JSONResponse(content={"detail": "bad request"}, status_code=400)
    if item is None:
        return JSONResponse(content={"detail": "lesson not found"}, status_code=404)
    learning.invalidate_cache()
    asyncio.create_task(_sb.audit(actor=INBOX_ADMIN_NAME, action="lesson_" + item["status"], detail={"lesson": item["text"], "kind": item["kind"]}))
    return JSONResponse(content={"ok": True, "lesson": item})


@app.delete("/api/inbox/learning/lessons/{lesson_id}")
async def api_inbox_lesson_delete(lesson_id: str, request: Request) -> JSONResponse:
    """Delete a lesson permanently."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    await feedback_store.delete_lesson(lesson_id)
    learning.invalidate_cache()
    return JSONResponse(content={"ok": True})


@app.get("/inbox/learning")
async def web_inbox_learning(request: Request) -> Response:
    resp, ctx = _logged_in_page_ctx(request)
    if resp:
        return resp
    return _render_template("learning.html", **ctx)


@app.get("/api/inbox/chats/{e164}/suggestion")
async def api_inbox_ai_suggestion(e164: str, request: Request) -> JSONResponse:
    """Return a suggested AI reply to the buyer's latest message.

    Implementation details (hard safety rules from README/PETROBIND_SHARED_INBOX.md):
      - Runs the LLM pipeline in `draft_only=True` mode: deep-clones session,
        skips ALL notify emails, skips ALL save_session writes (so primary
        Upstash + in-mem stores are never touched).
      - LRU in-memory cache keyed by (e164, last_buyer_wamid), TTL 300s so UI
        refresh doesn't burn duplicate LLM tokens.
      - Cache is evicted lazily on every call — no background task needed.
    """
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail

    # 1) Find last buyer message wamid + text for cache key + pipeline input
    try:
        msgs = await _sb.thread_messages(e164, limit=100)
    except Exception as exc:
        print(f"[INBOX] suggestion thread_messages {e164!r} error: {type(exc).__name__}")
        return JSONResponse(
            content={"detail": f"Database error: {type(exc).__name__}"},
            status_code=502,
        )
    # msgs ordered ASC by created_at (oldest→newest from supabase_client). Find newest buyer direction.
    last_buyer_msg: Optional[Dict[str, Any]] = None
    for m in reversed(msgs):
        if m.get("direction") == "buyer":
            last_buyer_msg = m
            break
    if not last_buyer_msg:
        return JSONResponse(
            content={"detail": "No buyer messages found for this thread"},
            status_code=404,
        )
    last_buyer_wamid = last_buyer_msg.get("wamid") or ""
    last_buyer_text = (last_buyer_msg.get("text") or "").strip()
    if not last_buyer_text:
        return JSONResponse(
            content={"detail": "Last buyer message has no text body"},
            status_code=404,
        )

    # 2) Cache lookup
    _evict_stale_suggestions()
    cache_key: Tuple[str, str] = (e164, last_buyer_wamid)
    cached = _SUGGESTION_CACHE.get(cache_key)
    if cached:
        _ts, suggestion = cached
        return JSONResponse(content={
            "suggestion_text": suggestion,
            "last_buyer_wamid": last_buyer_wamid,
            "cached": True,
            "age_secs": int(time.monotonic() - _ts),
        })

    # 3) Cache miss → run draft_only pipeline
    try:
        suggestion = await llm_assistant.handle_incoming_message(
            phone_number=e164,
            inbound_text=last_buyer_text,
            draft_only=True,
        )
    except Exception as exc:
        print(f"[INBOX] suggestion llm pipeline error for {e164!r}: {type(exc).__name__}: {exc!s}")
        return JSONResponse(
            content={"detail": f"LLM pipeline error: {type(exc).__name__}: {exc!s}"},
            status_code=502,
        )
    if not suggestion:
        suggestion = (
            "Thanks for your message, let me get back to you on that shortly. "
            "It'll help if you can share the target product (e.g. Bitumen 60/70), "
            "destination port, and roughly how much volume you need per shipment."
        )
    _SUGGESTION_CACHE[cache_key] = (time.monotonic(), suggestion)
    return JSONResponse(content={
        "suggestion_text": suggestion,
        "last_buyer_wamid": last_buyer_wamid,
        "cached": False,
    })


@app.delete("/api/inbox/chats/{e164}/suggestion")
async def api_inbox_evict_suggestion(e164: str, request: Request) -> JSONResponse:
    """Evict cached suggestion for a thread (called when user Discards a pill
    so reload will trigger a fresh LLM call)."""
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    # Drop every cache key starting with this e164
    dropped = 0
    for k in list(_SUGGESTION_CACHE.keys()):
        if k[0] == e164:
            _SUGGESTION_CACHE.pop(k, None)
            dropped += 1
    _evict_stale_suggestions()
    return JSONResponse(content={"ok": True, "evicted_entries": dropped})


@app.get("/api/inbox/window-check/{e164}")
async def api_inbox_window_check(e164: str, request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        from urllib.parse import unquote
        e164_decoded = unquote(e164)
    except Exception:
        e164_decoded = e164
    last_buyer_at_unix: Optional[float] = None
    try:
        last_buyer_at_unix = await _sb.get_last_buyer_message_at(e164_decoded)  # type: ignore[attr-defined]
    except Exception:
        last_buyer_at_unix = None
    if last_buyer_at_unix is None:
        try:
            await conversation_store.redis_scan_all_sessions()
            _s = _find_session_by_number(e164_decoded)
            if _s is not None:
                last_buyer_at_unix = _s.last_buyer_ts or _s.last_activity_ts
        except Exception:
            pass
    inside_24h = bool(
        last_buyer_at_unix is not None
        and (time.time() - float(last_buyer_at_unix)) < 86400
    )
    window_closes_at: Optional[float] = (
        float(last_buyer_at_unix) + 86400 if last_buyer_at_unix is not None else None
    )

    def _iso_or_none(ts: Optional[float]) -> Optional[str]:
        if ts is None:
            return None
        try:
            return (
                datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except Exception:
            return None

    return JSONResponse(content={
        "inside_24h_window": inside_24h,
        "last_buyer_at_unix_ts": last_buyer_at_unix,
        "last_buyer_at_iso": _iso_or_none(last_buyer_at_unix),
        "window_closes_at_unix_ts": window_closes_at,
        "window_closes_at_iso": _iso_or_none(window_closes_at),
    })


@app.get("/api/inbox/templates")
async def api_inbox_list_templates(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return JSONResponse(
            content={
                "detail": (
                    "WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN missing — "
                    "cannot fetch templates from Meta Graph"
                )
            },
            status_code=503,
        )
    global TEMPLATES_CACHE
    now = time.time()
    if (
        TEMPLATES_CACHE is not None
        and (now - TEMPLATES_CACHE[0]) < TEMPLATES_CACHE_TTL
    ):
        return JSONResponse(content={
            "templates": TEMPLATES_CACHE[1],
            "cached": True,
            "cached_at_unix": TEMPLATES_CACHE[0],
        })
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/message_templates",
                params={
                    "fields": "name,category,language,status,components",
                    "status": "APPROVED",
                },
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            )
            if not (200 <= r.status_code < 300):
                return JSONResponse(
                    content={
                        "detail": "Meta Graph templates call failed",
                        "http_status": r.status_code,
                        "body_preview": r.text[:300],
                    },
                    status_code=502,
                )
            data = r.json().get("data", []) or []
            approved = [t for t in data if t.get("status") == "APPROVED"] if data else []
            templates = approved or data
            simplified: List[Dict[str, Any]] = []
            import re as _re
            for tpl in templates:
                t: Dict[str, Any] = {
                    "name": tpl.get("name"),
                    "category": tpl.get("category"),
                    "language": tpl.get("language"),
                    "status": tpl.get("status"),
                    "components": [],
                }
                comps = tpl.get("components") or []
                for comp in comps:
                    ctype = comp.get("type")
                    ctext = comp.get("text") or ""
                    placeholders: List[str] = _re.findall(r"\{\{(\d+)\}\}", str(ctext))
                    t["components"].append({
                        "type": ctype,
                        "text": ctext,
                        "parameters": placeholders,
                    })
                simplified.append(t)
            TEMPLATES_CACHE = (now, simplified)
            return JSONResponse(content={
                "templates": simplified,
                "cached": False,
                "cached_at_unix": now,
            })
    except Exception as exc:
        return JSONResponse(
            content={
                "detail": f"Meta Graph templates call exception: {type(exc).__name__}: {exc!s}",
            },
            status_code=502,
        )


@app.delete("/api/inbox/templates/cache")
async def api_inbox_evict_templates_cache(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    global TEMPLATES_CACHE
    TEMPLATES_CACHE = None
    return JSONResponse(content={"evicted": True})


@app.post("/api/inbox/new-conversation")
async def api_inbox_new_conversation(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    e164_raw = (body.get("e164") or "").strip()
    if not e164_raw:
        raise HTTPException(status_code=422, detail="'e164' is required")
    try:
        from urllib.parse import unquote
        e164 = unquote(e164_raw)
    except Exception:
        e164 = e164_raw

    ok, retry = _global_proactive_rate_limiter("global")
    if not ok:
        return JSONResponse(
            content={"detail": f"Global rate limit exceeded — retry after {retry:.1f}s"},
            status_code=429,
        )
    ok, retry = _per_e164_rate_limiter(e164)
    if not ok:
        return JSONResponse(
            content={"detail": f"Per-e164 rate limit exceeded — retry after {retry:.1f}s"},
            status_code=429,
        )

    text = (body.get("text") or "").strip() or None
    mode = (body.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "freeform_force", "template_force"}:
        mode = "auto"
    template_name = (body.get("template_name") or "").strip() or None
    template_params = body.get("template_params")
    if template_params is None:
        template_params = []
    language = (body.get("language") or "en_US").strip() or "en_US"
    category = (body.get("category") or "").strip() or None

    last_buyer_at: Optional[float] = None
    try:
        last_buyer_at = await _sb.get_last_buyer_message_at(e164)  # type: ignore[attr-defined]
    except Exception:
        last_buyer_at = None
    inside_24h = bool(last_buyer_at is not None and (time.time() - float(last_buyer_at)) < 86400)

    my_session_id = (
        request.headers.get("X-Inbox-Session-Id")
        or request.headers.get("x-inbox-session-id")
        or f"newconv-{secrets.token_hex(6)}"
    )

    if mode == "freeform_force" or (mode == "auto" and inside_24h):
        if not text:
            raise HTTPException(
                status_code=400,
                detail=(
                    "'text' is required for freeform send (mode=freeform_force or "
                    "mode=auto inside 24h window)"
                ),
            )
        held = await _claim_is_held_by_other(e164, my_session_id=my_session_id)
        if not held:
            try:
                await _sb.claim_acquire(  # type: ignore[attr-defined]
                    e164=e164,
                    held_by=INBOX_ADMIN_NAME,
                    session_id=my_session_id,
                    ttl_seconds=120,
                )
            except Exception:
                pass
        elif held and held.get("session_id") != my_session_id:
            return JSONResponse(
                content={
                    "success": False,
                    "inside_24h_window": inside_24h,
                    "detail": (
                        f"Chat claimed by {held.get('held_by') or 'another admin'}"
                    ),
                    "held_by": held.get("held_by"),
                },
                status_code=409,
            )
        try:
            result = await send_whatsapp_text(
                to=e164,
                text=text,
                sender_direction="human",
            )
        except RuntimeError as exc:
            return JSONResponse(
                content={
                    "success": False,
                    "inside_24h_window": inside_24h,
                    "send_type": "freeform",
                    "error": str(exc),
                },
                status_code=502,
            )
        sent_id = ((result.get("messages") or [{}])[0].get("id")) if result else None
        return JSONResponse(content={
            "success": True,
            "send_type": "freeform",
            "inside_24h_window": inside_24h,
            "sent_id": sent_id,
            "result": result,
        })

    if mode == "template_force" or (mode == "auto" and not inside_24h):
        if mode == "auto" and not template_name:
            templates_ref: List[Dict[str, Any]] = []
            if TEMPLATES_CACHE is not None and (time.time() - TEMPLATES_CACHE[0]) < TEMPLATES_CACHE_TTL:
                templates_ref = list(TEMPLATES_CACHE[1])
            else:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        r = await client.get(
                            f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/message_templates",
                            params={
                                "fields": "name,category,language,status,components",
                                "status": "APPROVED",
                            },
                            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                        )
                        if 200 <= r.status_code < 300:
                            d = r.json().get("data", []) or []
                            TEMPLATES_CACHE = (time.time(), d)
                            templates_ref = d
                except Exception:
                    templates_ref = []
            return JSONResponse(
                content={
                    "inside_24h_window": False,
                    "detail": (
                        "Outside 24h window — send with Meta-approved template via "
                        "template_force mode"
                    ),
                    "templates": templates_ref,
                },
                status_code=422,
            )
        if mode == "template_force" and not template_name:
            raise HTTPException(
                status_code=400,
                detail="'template_name' is required for mode=template_force",
            )
        try:
            result = await send_whatsapp_template(
                e164,
                str(template_name),
                language_code=language,
                params=template_params,
                sender_direction="human",
                category=category,
            )
        except RuntimeError as exc:
            return JSONResponse(
                content={
                    "success": False,
                    "inside_24h_window": inside_24h,
                    "send_type": "template",
                    "error": str(exc),
                },
                status_code=502,
            )
        sent_id = (result.get("messages") or [{}])[0].get("id") if result.get("messages") else None
        return JSONResponse(content={
            "success": True,
            "send_type": "template",
            "inside_24h_window": inside_24h,
            "sent_id": sent_id,
            "result": result,
        })

    return JSONResponse(
        content={"detail": "unhandled mode", "mode": mode},
        status_code=500,
    )


@app.post("/api/inbox/send-template")
async def api_inbox_send_template(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    e164_raw = (body.get("e164") or "").strip()
    template_name = (body.get("template_name") or "").strip()
    if not e164_raw or not template_name:
        raise HTTPException(
            status_code=422,
            detail="'e164' and 'template_name' are required",
        )
    try:
        from urllib.parse import unquote
        e164 = unquote(e164_raw)
    except Exception:
        e164 = e164_raw

    ok, retry = _global_proactive_rate_limiter("global")
    if not ok:
        return JSONResponse(
            content={"detail": f"Global rate limit exceeded — retry after {retry:.1f}s"},
            status_code=429,
        )
    ok, retry = _per_e164_rate_limiter(e164)
    if not ok:
        return JSONResponse(
            content={"detail": f"Per-e164 rate limit exceeded — retry after {retry:.1f}s"},
            status_code=429,
        )

    language = (body.get("language") or "en_US").strip() or "en_US"
    params = body.get("params") or body.get("template_params") or []
    category = (body.get("category") or "").strip() or None
    sender_dir = (body.get("sender_direction") or "human").strip() or "human"
    try:
        result = await send_whatsapp_template(
            e164,
            template_name,
            language_code=language,
            params=params,
            sender_direction=sender_dir,
            category=category,
        )
    except RuntimeError as exc:
        return JSONResponse(
            content={"success": False, "error": str(exc)},
            status_code=502,
        )
    await _clear_needs_human(e164)
    return JSONResponse(content={"success": True, **result})


# =========================================================================
# ADMIN OPS CONSOLE — /inbox/admin + /api/inbox/admin/*
# New for v2026.10 so user can VISUALIZE BOTH frontend chat + backend state
# (Redis sessions, Supabase claims, outbound_schedules, template cache,
# scheduler timezone/weekend maps) on a single page.  Same auth as inbox:
# INBOX_ADMIN_TOKEN password via /inbox/login → session cookie + Bearer.
# =========================================================================

@app.get("/inbox/admin", response_class=HTMLResponse)
async def web_inbox_admin(request: Request) -> Response:
    resp, ctx = _logged_in_page_ctx(request)
    if resp:
        return resp
    return _render_template("admin.html", **ctx)


@app.get("/inbox/architecture", response_class=HTMLResponse)
async def web_inbox_architecture(request: Request) -> Response:
    resp, ctx = _logged_in_page_ctx(request)
    if resp:
        return resp
    return _render_template("architecture.html", **ctx)


@app.get("/api/inbox/admin/dashboard")
async def api_inbox_admin_dashboard(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    now_unix = time.time()
    # --- Panel 1: health endpoint equivalent (mirrors /health/checks dict) ---
    try:
        _llm_ready: bool = False
        try:
            _ir = getattr(llm_assistant, "is_ready", None)
            _llm_ready = bool(_ir()) if callable(_ir) else bool(
                os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
            )
        except Exception:
            _llm_ready = bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY"))
        _rag_ready: bool = False
        try:
            _fn = getattr(llm_assistant, "index_is_ready", None)
            if callable(_fn):
                _rag_ready = bool(_fn())
            else:
                try:
                    from rag import index_is_ready as _rag_fn  # type: ignore
                    _rag_ready = bool(_rag_fn())
                except Exception:
                    _rag_ready = True
        except Exception:
            _rag_ready = True
        checks_p1: Dict[str, Any] = {
            "whatsapp_env": bool(ACCESS_TOKEN and PHONE_NUMBER_ID and VERIFY_TOKEN),
            "llm_ready": _llm_ready,
            "rag_index_ready": _rag_ready,
            "supabase_inbox": _sb_enabled(),
            "inbox_login_configured": bool(INBOX_ADMIN_TOKEN),
            "booking_configured": bool(booking.is_configured()),
            "email_configured": bool(
                (os.getenv("GMAIL_RELAY_URL") and os.getenv("GMAIL_RELAY_SECRET"))
                or (os.getenv("SMTP_USERNAME") and os.getenv("SMTP_PASSWORD") and os.getenv("SALES_EMAIL"))
            ),
            "redis_scan_available": False,
            "followups_configured": bool((os.getenv("FOLLOWUPS_CRON_TOKEN") or "").strip()),
        }
        try:
            from conversation_store import _last_redis_full_scan_ts  # type: ignore
            redis_env_ok = bool(os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"))
            checks_p1["redis_scan_available"] = (_last_redis_full_scan_ts is not None) or redis_env_ok
        except Exception:
            checks_p1["redis_scan_available"] = False
    except Exception as e:
        checks_p1 = {"_error": f"build checks: {e!r}"}

    # --- Panel 2: Redis/In-process/DB session counts + process age ---
    start_ts: Optional[float] = None
    try:
        from conversation_store import _PROCESS_START_TS as _pstart  # type: ignore
        start_ts = float(_pstart)
    except Exception:
        start_ts = None
    in_process_count = len(getattr(conversation_store, "_SESSIONS", {}) or {})
    db_sessions_count: int = 0
    redis_upstash_env_count: Optional[int] = None
    if _sb is not None and _sb_enabled():
        try:
            db_sessions_count = int(await _sb.admin_sessions_count_db() or 0)
        except Exception:
            db_sessions_count = 0
    # Upstash key count (best-effort /keys/session:*):
    try:
        ru = os.getenv("UPSTASH_REDIS_REST_URL") or ""
        rk = os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
        if ru and rk:
            async with httpx.AsyncClient(timeout=5.0) as hc:
                rr = await hc.get(f"{ru}/keys/session:*", headers={"Authorization": f"Bearer {rk}"})
                if rr.status_code == 200:
                    jj = rr.json().get("result") or []
                    if isinstance(jj, list):
                        redis_upstash_env_count = len(jj)
    except Exception:
        redis_upstash_env_count = None

    # --- Panel 3: Active inbox claims (released_at null) ---
    claims: List[Dict[str, Any]] = []
    if _sb is not None and _sb_enabled():
        try:
            claims = list(await _sb.admin_list_active_inbox_claims(limit=50) or [])
        except Exception:
            claims = []

    # --- Panel 4: Outbound schedules — status counts + pending sample ---
    sched_status: Dict[str, int] = {}
    sched_pending: List[Dict[str, Any]] = []
    if _sb is not None and _sb_enabled():
        try:
            sched_status = dict(await _sb.admin_outbound_schedules_status_counts() or {})
        except Exception:
            sched_status = {}
        try:
            sched_pending = list(await _sb.admin_outbound_schedules_pending_sample(limit=30) or [])
        except Exception:
            sched_pending = []

    # --- Panel 5: Templates cache metadata + list with truncated component bodies ---
    templates_meta: Dict[str, Any] = {"configured": bool(ACCESS_TOKEN and PHONE_NUMBER_ID)}
    templates_list: List[Dict[str, Any]] = []
    templates_cached_at: Optional[float] = None
    try:
        if TEMPLATES_CACHE is not None and isinstance(TEMPLATES_CACHE, tuple) and len(TEMPLATES_CACHE) == 2:
            templates_cached_at = float(TEMPLATES_CACHE[0])
            templates_list = list(TEMPLATES_CACHE[1] or [])
    except Exception:
        templates_cached_at = None
        templates_list = []
    templates_meta.update({
        "cached": templates_cached_at is not None,
        "cached_at_unix": templates_cached_at,
        "ttl_seconds": TEMPLATES_CACHE_TTL,
        "age_seconds": (now_unix - templates_cached_at) if templates_cached_at else None,
        "count": len(templates_list),
    })
    # Summary form of each template (avoid huge bodies):
    templates_summary: List[Dict[str, Any]] = []
    for t in templates_list:
        if not isinstance(t, dict):
            continue
        params = t.get("parameters") or t.get("parameter_count") or 0
        if isinstance(params, list):
            params = len(params)
        templates_summary.append({
            "name": t.get("name"),
            "category": t.get("category"),
            "language": t.get("language") or t.get("locale"),
            "status": t.get("status"),
            "params_n": int(params or 0),
        })

    # --- Panel 6: Scheduler metadata (timezone/weekend maps) + "what time is it right now buyer-local" quick check ---
    sample_prefixes = ["65", "60", "971", "966", "1", "44", "61"]  # SG MY AE SA US UK AU
    now_local_samples: List[Dict[str, Any]] = []
    try:
        import datetime as _dt
        for pref in sample_prefixes:
            off = float(getattr(scheduler, "COUNTRY_TZ_OFFSET", {}).get(pref, 0.0) or 0.0)
            weekend_name = {
                (4, 5): "Fri-Sat (Middle East)",
                (5, 6): "Sat-Sun (Global default)",
            }.get(tuple(getattr(scheduler, "COUNTRY_WEEKEND_MAP", {}).get(pref, (5, 6))), "custom")
            local_now = _dt.datetime.utcfromtimestamp(now_unix + off * 3600)
            now_local_samples.append({
                "prefix": "+" + pref,
                "tz_offset_hours": off,
                "weekend": weekend_name,
                "local_now_iso": local_now.strftime("%Y-%m-%d %H:%M:%S"),
                "local_hour": local_now.hour,
                "inside_quiet_22_07": (local_now.hour >= 22 or local_now.hour < 7),
                "weekday_idx_mon0": local_now.weekday(),
            })
    except Exception:
        now_local_samples = []
    scheduler_meta: Dict[str, Any] = {
        "country_tz_count": len(getattr(scheduler, "COUNTRY_TZ_OFFSET", {}) or {}),
        "country_weekend_count": len(getattr(scheduler, "COUNTRY_WEEKEND_MAP", {}) or {}),
        "environment_followups_weekend_sends_ok": (
            (os.getenv("FOLLOWUPS_WEEKEND_SENDS_OK") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
        ),
        "followups_cron_configured": bool(FOLLOWUPS_CRON_TOKEN),
        "sample_prefix_local_now": now_local_samples,
    }

    # --- Process metadata ---
    process_meta: Dict[str, Any] = {
        "pid": os.getpid(),
        "started_at_unix": start_ts,
        "uptime_seconds": (now_unix - start_ts) if start_ts else None,
        "working_dir": os.path.abspath(os.getcwd()),
        "python_branch": __import__("platform").python_version(),
    }

    return JSONResponse(content={
        "generated_at_unix": now_unix,
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_unix)),
        "checks": checks_p1,
        "sessions": {
            "in_process_cache": in_process_count,
            "supabase_db_mirror": db_sessions_count,
            "redis_upstash_persisted": redis_upstash_env_count,
        },
        "claims": claims,
        "outbound_schedules": {
            "status_counts": sched_status,
            "pending_sample": sched_pending,
        },
        "templates": {
            "meta": templates_meta,
            "list": templates_summary,
        },
        "scheduler": scheduler_meta,
        "process": process_meta,
    })


@app.post("/api/inbox/admin/force-redis-scan")
async def api_inbox_admin_force_redis_scan(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    try:
        added = int(await conversation_store.redis_scan_all_sessions(force=True) or 0)
    except Exception as exc:
        return JSONResponse(
            content={"ok": False, "error": f"{type(exc).__name__}: {exc!s}"},
            status_code=500,
        )
    return JSONResponse(content={"ok": True, "newly_hydrated_from_redis": added})


@app.post("/api/inbox/admin/evict-templates-cache")
async def api_inbox_admin_evict_templates(request: Request) -> JSONResponse:
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    global TEMPLATES_CACHE
    previous = TEMPLATES_CACHE is not None
    TEMPLATES_CACHE = None
    return JSONResponse(content={"ok": True, "evicted_previous_cache": previous})


@app.post("/api/inbox/admin/run-followups-scan")
async def api_inbox_admin_run_followups_scan(request: Request) -> JSONResponse:
    """Admin-button equivalent of the Render cron POST /followups-scan.

    Uses FOLLOWUPS_CRON_TOKEN internally (same validation path the cron route
    uses), gated by INBOX_ADMIN_TOKEN so only the ops-console user can press
    the button.  Calls the route in-process via TestClient so all guards
    (token check, Supabase enabled check, Redis scan, claim mutex, quiet
    window defer) run exactly as they would for the real Render cron hit.
    Returns exactly the same JSON payload the cron endpoint returns (wrapped).
    """
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    if not FOLLOWUPS_CRON_TOKEN:
        return JSONResponse(
            content={"ok": False, "error": "FOLLOWUPS_CRON_TOKEN env var not set on server — cannot trigger followups cron"},
            status_code=503,
        )
    # NOTE: route is fully idempotent so calling via self-HTTP TestClient is
    # perfectly safe.  This avoids needing to bind the endpoint closure to a
    # fake Request with a valid asgi scope for the async pattern of the inner
    # guard code.  Starlette TestClient runs on a temporary in-memory
    # transport — no network socket opened, no host/port required.
    from starlette.testclient import TestClient as _TC  # type: ignore
    with _TC(app, raise_server_exceptions=False) as tc:
        rresp = tc.post(
            "/followups-scan",
            headers={"Authorization": f"Bearer {FOLLOWUPS_CRON_TOKEN}"},
        )
        try:
            body = rresp.json()
        except Exception:
            body = {"raw": rresp.text[:500]}
        status = rresp.status_code if isinstance(rresp.status_code, int) else 500
        return JSONResponse(
            content={
                "ok": 200 <= status < 300,
                "http_status": status,
                "followups_scan_result": body,
            },
            status_code=status if 200 <= status < 300 else 502,
        )


@app.post("/api/inbox/admin/flush-scheduled-sends")
async def api_inbox_admin_flush_scheduled(request: Request) -> JSONResponse:
    """Admin-button equivalent of the Render cron POST /scheduled-send-flush.

    Same pattern as followups above — in-process TestClient self-call so the
    full idempotent claim_next_pending_scheduled_batch → send path runs.
    """
    fail = _requires_inbox_bearer(request)
    if fail:
        return fail
    if not FOLLOWUPS_CRON_TOKEN:
        return JSONResponse(
            content={"ok": False, "error": "FOLLOWUPS_CRON_TOKEN env var not set on server — cannot trigger scheduled flush"},
            status_code=503,
        )
    from starlette.testclient import TestClient as _TC  # type: ignore
    with _TC(app, raise_server_exceptions=False) as tc:
        rresp = tc.post(
            "/scheduled-send-flush",
            headers={"Authorization": f"Bearer {FOLLOWUPS_CRON_TOKEN}"},
        )
        try:
            body = rresp.json()
        except Exception:
            body = {"raw": rresp.text[:500]}
        status = rresp.status_code if isinstance(rresp.status_code, int) else 500
        return JSONResponse(
            content={
                "ok": 200 <= status < 300,
                "http_status": status,
                "flush_result": body,
            },
            status_code=status if 200 <= status < 300 else 502,
        )


@app.api_route(
    "/{path_name:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def probe_catchall(request: Request, path_name: str) -> JSONResponse:
    # IMPORTANT: FastAPI wildcard routes match very eagerly. We must RAISE a
    # real 404 for /api/inbox/* and /inbox/* paths so FastAPI's default
    # 404/405 handling takes over for our real inbox routes above, rather
    # than this probe-catchall returning 200 for everything.
    p = (path_name or "").lstrip("/")
    if (
        p.startswith("api/inbox/")
        or p.startswith("inbox/")
        or p == "inbox"
        or p == "api/inbox"
        or p.startswith("api/inbox")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
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
