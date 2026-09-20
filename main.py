import os
from typing import Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse

load_dotenv()

app = FastAPI(title="WhatsApp Webhook Gateway", version="1.0.0")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")


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
        return PlainTextResponse(content=hub_challenge, status_code=status.HTTP_200_OK)

    if hub_mode is None and hub_verify_token is None and hub_challenge is None:
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
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    obj = payload.get("object")
    if obj != "whatsapp_business_account":
        print(f"[WARN] Received webhook with unexpected object={obj!r} — still acking 200")

    try:
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                for message in value.get("messages", []) or []:
                    _process_message(message)
                for st in value.get("statuses", []) or []:
                    _process_status(st)
    except Exception as exc:  # pragma: no cover - defensive, never fail the webhook ack
        print(f"[ERROR] Failed while processing webhook payload: {exc!r}")

    return JSONResponse(
        content={"status": "success", "message": "EVENT_RECEIVED"},
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
