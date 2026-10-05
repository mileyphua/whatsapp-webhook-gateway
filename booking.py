"""Cal.com booking integration.

Two pieces (per PLAN Part 3.2):
  1. get_booking_link() returns the assistant's shared booking URL for the
     model to share in chat when the buyer shows scheduling intent.
  2. handle_cal_webhook(payload) is wired to POST /cal-webhook in main.py.
     It logs the booking and calls notify.send_booking_email so the sales
     desk always knows a call was booked.

We intentionally do NOT use the Cal.com API to create events ourselves —
Cal.com handles all calendar sync / timezones / reminder emails. This module
only exposes the shareable link and reacts to BOOKING_CREATED webhooks.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

import notify

CAL_COM_BOOKING_LINK = os.getenv("CAL_COM_BOOKING_LINK", "").strip()
CAL_COM_API_KEY = os.getenv("CAL_COM_API_KEY", "").strip()


def _find_session_for_phone(phone: Any):
    """Best-effort lookup into conversation_store._SESSIONS to find a matching
    WhatsApp session (so we can stamp booking_confirmed_at on it, which
    suppresses the booking follow-up nudge)."""
    if not phone:
        return None
    try:
        import conversation_store  # local import avoids circular-at-import-time
    except Exception:
        return None
    phone_str = str(phone).lstrip(" ").rstrip()
    # First try exact match (the strings match the WhatsApp `from` field E.164).
    exact = conversation_store._SESSIONS.get(phone_str)
    if exact:
        return exact
    # Then try normalize-numeric match (strip +, spaces, dashes — compare last-9
    # to cover country-code diffs). Rarely needed but harmless.
    norm = "".join(ch for ch in phone_str if ch.isdigit())
    if len(norm) < 7:
        return None
    for pn, sess in conversation_store._SESSIONS.items():
        sess_norm = "".join(ch for ch in pn if ch.isdigit())
        if sess_norm.endswith(norm[-9:]) or norm.endswith(sess_norm[-9:]):
            return sess
    return None


def _flatten(payload: Mapping[str, Any]) -> dict:
    """Cal.com wraps the booking in payload["payload"]; accept both that and a flat dict."""
    d = dict(payload)
    inner = payload.get("payload")
    if isinstance(inner, dict):
        d.update(inner)
    return d


def _val(x: Any) -> Any:
    """Cal.com `responses` entries look like {"label":..., "value":...}."""
    return x.get("value") if isinstance(x, dict) else x


def find_session_for_booking(payload: Mapping[str, Any]):
    """Match a Cal.com booking to a WhatsApp chat: by WhatsApp metadata, then by the attendee's phone number
    (last 9 digits, so +/spaces/country code don't matter), then by the e-mail the buyer gave in the chat."""
    d = _flatten(payload)
    meta = d.get("metadata") or (d.get("booking") or {}).get("metadata") or {}
    responses = d.get("responses") or {}
    attendees = d.get("attendees") or []
    phones: list = [meta.get("whatsapp"), meta.get("phone_number"), meta.get("buyer_whatsapp"),
                    _val(responses.get("attendeePhoneNumber")), _val(responses.get("phone")), d.get("attendeePhoneNumber")]
    emails: list = [_val(responses.get("email"))]
    for a in attendees:
        if isinstance(a, dict):
            phones += [a.get("phoneNumber"), a.get("phone")]
            emails.append(a.get("email"))
    for ph in phones:
        sess = _find_session_for_phone(ph) if ph else None
        if sess:
            return sess
    try:
        import conversation_store
    except Exception:
        return None
    wanted = {str(e).strip().lower() for e in emails if e}
    for sess in conversation_store._SESSIONS.values():
        mail = (sess.inquiry.contact_email or "").strip().lower()
        if mail and mail in wanted:
            return sess
    return None


async def _stamp_session_booking(phone: Any, payload: Mapping[str, Any]) -> None:
    """After Cal.com BOOKING_CREATED, stamp matching ConversationSession so
    booking-follow-up nudges don't fire to someone who already booked."""
    import time as _t
    sess = _find_session_for_phone(phone)
    if not sess:
        return
    booking_fields: dict = {}
    for key in (
        "uid", "title", "startTime", "endTime", "eventTitle", "event_type_title",
        "start", "end", "attendees", "metadata", "bookingUrl", "notes",
    ):
        val = payload.get(key)
        if val not in (None, "", [], {}):
            booking_fields[key] = val
    booking_block = payload.get("booking") or {}
    if isinstance(booking_block, dict) and booking_block:
        booking_fields["booking"] = {
            k: v for k, v in booking_block.items()
            if k in ("id", "startTime", "endTime", "status", "metadata")
        }
    sess.booking_confirmed_at = _t.time()
    sess.booking_details.update(booking_fields)
    import conversation_store
    await conversation_store.save_session(sess)


def is_configured() -> bool:
    """True if we have at least a booking link to share in chat."""
    return bool(CAL_COM_BOOKING_LINK)


def get_booking_link(*, message: str | None = None, phone: str | None = None,
                     name: str | None = None, email: str | None = None) -> str:
    """Return the shareable booking link, plus a prompt sentence if requested.

    With `phone` the link carries the buyer's WhatsApp number (as booking metadata and as the prefilled phone
    field) and, when known, their name/email. That is what lets the /cal-webhook tell WHICH chat a booking
    belongs to, so nobody who already booked gets a reminder.

    If the env var isn't set yet (user hasn't supplied Cal.com details),
    returns an empty string so the model falls back to handoff instead of
    sharing a broken link.
    """
    if not CAL_COM_BOOKING_LINK:
        return ""
    url = CAL_COM_BOOKING_LINK
    params: list = []
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits:
        params += [("metadata[whatsapp]", digits), ("attendeePhoneNumber", "+" + digits)]
    if name:
        params.append(("name", name.strip()))
    if email:
        params.append(("email", email.strip()))
    if params:
        from urllib.parse import quote, urlencode
        url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)
    if message:
        return f"{message} {url}"
    return url


async def handle_cal_webhook(payload: Mapping[str, Any]) -> tuple[str, int]:
    """POST /cal-webhook handler. Returns (body_text, status_code).

    Cal.com signs webhooks with the secret set in their dashboard. We log,
    but don't enforce signature verification in v1 — if the env var
    CAL_COM_WEBHOOK_SECRET is present we'll additionally check the
    x-cal-signature-256 header in the FastAPI wrapper.
    """
    if not isinstance(payload, dict):
        return "invalid payload", 400

    trigger = str(
        payload.get("triggerEvent")
        or payload.get("trigger_event")
        or payload.get("type")
        or payload.get("event")
        or "UNKNOWN"
    )
    created = trigger.upper() in {
        "BOOKING_CREATED",
        "booking.created",
        "BOOKING_REQUESTED",
    }

    print(
        f"[CAL WEBHOOK] trigger={trigger!r} keys={sorted([k for k in payload.keys() if isinstance(k, str)])[:20]}"
    )

    if created:
        d = _flatten(payload)
        sess = find_session_for_booking(payload)
        phone = sess.phone_number if sess else None
        attendee = next((a for a in (d.get("attendees") or []) if isinstance(a, dict)), {})
        detail = {"attendee_name": attendee.get("name"), "attendee_email": attendee.get("email"),
                  "title": d.get("title"), "start": d.get("startTime") or d.get("start")}
        if sess:
            # Stamp the matching session so booking reminders never go to someone who already booked.
            await _stamp_session_booking(sess.phone_number, d)
        try:
            import supabase_client as _sb
            await _sb.audit(actor="cal.com", action="booking_confirmed" if sess else "booking_unmatched",
                            e164=sess.phone_number if sess else None, detail=detail)
        except Exception as exc:
            print(f"[CAL WEBHOOK] audit skipped: {type(exc).__name__}: {exc!s}")
        await notify.send_booking_email(phone_number=phone, cal_payload=payload)
        return "booking recorded", 200

    # We only act on booking-created events today; still ack 200 OK so Cal.com
    # doesn't retry forever for cancellation/reschedule/etc. we don't need yet.
    return f"ignored trigger={trigger}", 200


__all__ = [
    "CAL_COM_BOOKING_LINK",
    "CAL_COM_API_KEY",
    "is_configured",
    "get_booking_link",
    "handle_cal_webhook",
]
