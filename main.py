import json
import os
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

load_dotenv()

app = FastAPI(title="WhatsApp Webhook Gateway", version="1.0.0")

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
                    _process_message(message)
                    text_body = None
                    if message.get("type") == "text":
                        text_body = (message.get("text") or {}).get("body")
                    await _echo_reply(
                        from_number=message.get("from"),
                        phone_number_id=pnid,
                        inbound_text=text_body,
                        reply_to_message_id=message.get("id"),
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
