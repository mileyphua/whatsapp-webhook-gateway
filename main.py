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
API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
GRAPH_URL = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"


@app.get("/")
async def root() -> JSONResponse:
    debug_token = VERIFY_TOKEN if not VERIFY_TOKEN else (
        VERIFY_TOKEN[:6] + "…" + VERIFY_TOKEN[-4:]
    )
    return JSONResponse(
        content={
            "app": "WhatsApp Webhook Gateway",
            "version": "1.0.0",
            "verify_token_loaded": bool(VERIFY_TOKEN),
            "verify_token_preview": debug_token,
            "phone_number_id_loaded": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
            "access_token_loaded": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")),
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
                for message in value.get("messages", []) or []:
                    _process_message(message)
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
    preview_url: bool = True,
    reply_to_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError(
            "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be set"
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

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(GRAPH_URL, json=payload, headers=headers)
        body = r.json() if r.content else {}
        if 200 <= r.status_code < 300:
            return body
        raise RuntimeError(
            f"WhatsApp API {r.status_code}: "
            f"{body.get('error', {}).get('message', r.text)}"
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

    try:
        result = await send_whatsapp_text(
            to=str(to),
            text=str(text),
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
