"""Async Supabase wrapper for Petrobind shared-inbox persistence layer.

Uses raw httpx (REST API) — NOT the official supabase-py client. Reason:
main.py already pins httpx 0.27.x via requirements.txt; adding the official
client pulls in httpx 0.28+ which would be a dependency churn risk on
existing Petrobind Gateway running code.

All public functions are SAFE NO-OP + print a single WARNING line when
SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are not set or endpoint is down.
Never raise to the caller — callers wrap us in 2s timeout anyway via
_persist_inbound_safe/_persist_outbound_safe in main.py.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

_SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""

ENABLED: bool = bool(_SUPABASE_URL and _SUPABASE_KEY)

_DEFAULT_TIMEOUT = httpx.Timeout(3.0, connect=1.5)

_HEADERS: Dict[str, str] = {
    "apikey": _SUPABASE_KEY,
    "Authorization": f"Bearer {_SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

_REST_BASE = f"{_SUPABASE_URL}/rest/v1" if ENABLED else ""


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)


# ================================================================
# recent_wamids dedup
# ================================================================

async def recent_wamid_seen(wamid: str) -> bool:
    if not (ENABLED and wamid):
        return False
    try:
        async with _client() as c:
            r = await c.get(
                f"{_REST_BASE}/recent_wamids",
                headers={**_HEADERS, "Prefer": "count=exact,head=true"},
                params={"wamid": f"eq.{wamid}"},
            )
            return int(r.headers.get("Content-Range", "*/-1").split("/")[-1] or "0") > 0
    except Exception:
        return False  # on error, trust the in-mem dedup; don't double block


async def recent_wamid_mark(wamid: str) -> None:
    if not (ENABLED and wamid):
        return
    try:
        async with _client() as c:
            await c.post(
                f"{_REST_BASE}/recent_wamids",
                headers={**_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json={"wamid": wamid},
            )
    except Exception:
        return


# ================================================================
# message persistence
# ================================================================

async def insert_inbound_message(msg: Dict[str, Any]) -> None:
    d = describe_inbound(msg)
    if not ENABLED:
        created = None
        try:
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(msg["timestamp"]))) if msg.get("timestamp") else None
        except Exception:
            pass
        _store_local(e164=str(msg.get("from") or ""), direction="buyer", text=d["text"], sent_id=msg.get("id"), media_type=d["media_type"],
                     media_id=d["media_id"], mime=d["mime"], filename=d["filename"], created_at=created)
        return
    wamid = msg.get("id")
    from_e164 = msg.get("from")
    msg_type = msg.get("type")
    text = d["text"] or (media_placeholder(d["media_type"], d["filename"]) if d["media_type"] else None)
    media_url = d["media_id"]
    payload_jsonb = {k: v for k, v in msg.items() if k not in {"id", "from", "type", "timestamp", "text"}}
    body = {
        "wamid": wamid,
        "direction": "buyer",
        "e164": from_e164,
        "text": text,
        "media_type": d["media_type"] or (msg_type if msg_type not in ("text", "interactive", "button", "location", "contacts", "reaction") else None),
        "media_url": media_url,
        "payload_jsonb": payload_jsonb,
    }
    if msg.get("timestamp"):
        try:
            body["created_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(msg["timestamp"]))
            )
        except Exception:
            pass
    async with _client() as c:
        await c.post(
            f"{_REST_BASE}/messages",
            headers={**_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=body,
        )


# Without Supabase (local development) the messages people send from the inbox are kept here (and in a small JSON
# file so a server restart does not lose them) so they still show in the thread and the chat list. Never used in
# production, where the `messages` table is the source of truth.
import json as _json
_LOCAL_OUTBOUND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".local_outbound.json")
_LOCAL_OUTBOUND: Dict[str, List[Dict[str, Any]]] = {}
_LOCAL_MAX_PER_CHAT = 500


def _digits(v: str) -> str:
    return "".join(ch for ch in (v or "") if ch.isdigit())


def _load_local_outbound() -> None:
    try:
        with open(_LOCAL_OUTBOUND_FILE, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        if isinstance(data, dict):
            _LOCAL_OUTBOUND.update({str(k): list(v) for k, v in data.items() if isinstance(v, list)})
    except Exception:
        pass


def _save_local_outbound() -> None:
    try:
        with open(_LOCAL_OUTBOUND_FILE, "w", encoding="utf-8") as fh:
            _json.dump(_LOCAL_OUTBOUND, fh)
    except Exception:
        pass


def local_outbound_messages(e164: str) -> List[Dict[str, Any]]:
    out = [dict(m) for m in _LOCAL_OUTBOUND.get(_digits(e164), [])]
    for m in out:
        if m.get("media_type") and m.get("text") in (media_placeholder(m["media_type"], m.get("filename")), f"📎 {m.get('filename')}"):
            m["text"] = ""             # the file itself is shown; no filler line above it
    return out


def local_outbound_chats() -> Dict[str, Dict[str, Any]]:
    """digits -> the newest locally stored message of each chat."""
    return {k: dict(v[-1]) for k, v in _LOCAL_OUTBOUND.items() if v}


def media_placeholder(media_type: Optional[str], filename: Optional[str] = None) -> str:
    """Readable stand-in text for a file without a caption (shown in the chat list; hidden inside the bubble)."""
    return {"image": "📷 Photo", "audio": "🎤 Voice message", "video": "🎥 Video"}.get(media_type or "") or f"📄 {filename or 'Document'}"


def describe_inbound(msg: Dict[str, Any]) -> Dict[str, Any]:
    """What an inbound WhatsApp message looks like in the inbox: readable text for every message type, plus the
    media id/filename/mime when it carries a file (so the thread can show the photo, voice note or document)."""
    t = str(msg.get("type") or "")
    blob = msg.get(t) if isinstance(msg.get(t), (dict, list)) else {}
    out: Dict[str, Any] = {"text": "", "media_type": None, "media_id": None, "filename": None, "mime": None}
    if t == "text":
        out["text"] = (msg.get("text") or {}).get("body") or ""
    elif t in ("image", "audio", "video", "document", "sticker"):
        out["media_type"] = "image" if t == "sticker" else t
        out["media_id"] = blob.get("id") or blob.get("link")
        out["mime"] = blob.get("mime_type")
        out["filename"] = blob.get("filename")
        out["text"] = blob.get("caption") or ""
    elif t == "button":
        out["text"] = blob.get("text") or blob.get("payload") or ""
    elif t == "interactive":
        kind = blob.get("type")
        pick = blob.get(kind) if kind else None
        out["text"] = (pick or {}).get("title") or (pick or {}).get("id") or "[interactive reply]"
    elif t == "location":
        name = " – ".join(x for x in (blob.get("name"), blob.get("address")) if x)
        out["text"] = f"📍 {name + ' ' if name else ''}({blob.get('latitude')}, {blob.get('longitude')})"
    elif t == "contacts":
        names = [((c.get("name") or {}).get("formatted_name") or "contact") for c in (blob if isinstance(blob, list) else [])]
        out["text"] = "👤 Shared contact: " + ", ".join(names) if names else "👤 Shared contact"
    elif t == "reaction":
        out["text"] = blob.get("emoji") or "(reaction removed)"
    else:
        out["text"] = f"[{t or 'unknown'} message – not supported in the inbox]"
    return out


def _store_local(*, e164: str, direction: str, text: str, sent_id: Optional[str] = None, errored: bool = False,
                 error_detail: Optional[str] = None, reply_to_wamid: Optional[str] = None, sent_by: Optional[str] = None,
                 media_type: Optional[str] = None, media_meta: Optional[Dict[str, Any]] = None, media_id: Optional[str] = None,
                 mime: Optional[str] = None, filename: Optional[str] = None, created_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    key = _digits(e164)
    if not key:
        return None
    meta = media_meta or {}
    rows = _LOCAL_OUTBOUND.setdefault(key, [])
    rows.append({
        "id": f"local-{key}-{len(rows) + 1}-{int(time.time() * 1000)}", "wamid": sent_id or "", "sent_id_from_graph": sent_id,
        "direction": direction if direction in ("ai", "human", "system", "buyer") else "system", "e164": key, "text": text,
        "reply_to_wamid": reply_to_wamid, "media_type": media_type, "media_id": media_id or meta.get("media_id"),
        "mime": mime or meta.get("mime"), "filename": filename or meta.get("filename"),
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "errored": errored,
        "error_detail": error_detail, "meta_statuses_jsonb": {}, "held_by": sent_by,
    })
    row = rows[-1]
    del rows[:-_LOCAL_MAX_PER_CHAT]
    _save_local_outbound()
    return row


def _store_local_outbound(*, e164: str, direction: str, text: str, sent_id: Optional[str], errored: bool, error_detail: Optional[str],
                          reply_to_wamid: Optional[str], sent_by: Optional[str], media_type: Optional[str],
                          media_meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return _store_local(e164=e164, direction=direction, text=text, sent_id=sent_id, errored=errored, error_detail=error_detail,
                 reply_to_wamid=reply_to_wamid, sent_by=sent_by, media_type=media_type, media_meta=media_meta)


def local_mark_status(wamid: str, status_value: str, errors: Optional[str]) -> bool:
    for rows in _LOCAL_OUTBOUND.values():
        for r in rows:
            if r.get("sent_id_from_graph") == wamid:
                r.setdefault("meta_statuses_jsonb", {})[status_value] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if errors:
                    r["errored"], r["error_detail"] = True, errors
                _save_local_outbound()
                return True
    return False


def _forget_local_outbound(variants: List[str]) -> None:
    changed = False
    for v in variants:
        if _LOCAL_OUTBOUND.pop(_digits(v), None) is not None:
            changed = True
    if changed:
        _save_local_outbound()


if not ENABLED:
    _load_local_outbound()


# ---------------------------------------------------------------------------------------------------------------
# Localhost -> Render. A local server has no Supabase key on purpose (it would be the database master key on a
# laptop). Instead, every message it sends is handed to the Render inbox (logged in as admin with the same
# INBOX_ADMIN_TOKEN), which saves it in Supabase. Set RENDER_INBOX_URL in the local .env to switch this on.
# Rows stay in the local file until Render has confirmed them, so a sleeping Render never loses a message.
# ---------------------------------------------------------------------------------------------------------------
_RENDER_COOKIE: Optional[str] = None
_FORWARD_DIRECTIONS = ("human", "ai")
# Forwarding is only switched on by a REAL running server (startup event), never by tests or scripts that merely import
# this module with the developer's .env loaded: otherwise their test messages would be written to the live database.
_FORWARD_ARMED = False


def arm_forwarding(on: bool = True) -> None:
    global _FORWARD_ARMED
    _FORWARD_ARMED = bool(on)


def _render_url() -> str:
    if not _FORWARD_ARMED:
        return ""
    return (os.getenv("RENDER_INBOX_URL") or "").strip().rstrip("/")


def _unsynced_rows() -> List[Dict[str, Any]]:
    if not _render_url():
        return []
    return [r for rows in _LOCAL_OUTBOUND.values() for r in rows
            if r.get("sent_id_from_graph") and r.get("direction") in _FORWARD_DIRECTIONS and r.get("sync") != "done"]


def local_unsynced_count() -> int:
    return len(_unsynced_rows())


def _forward_payload(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"e164": r["e164"], "direction": r["direction"], "text": r.get("text") or "", "wamid": r["sent_id_from_graph"],
            "created_at": r.get("created_at"), "sent_by": r.get("held_by"), "media_type": r.get("media_type"),
            "filename": r.get("filename"), "media_id": r.get("media_id"), "mime": r.get("mime"),
            "errored": bool(r.get("errored")), "error_detail": r.get("error_detail")}


async def _forward_to_render(rows: List[Dict[str, Any]]) -> bool:
    global _RENDER_COOKIE
    base, token = _render_url(), os.getenv("INBOX_ADMIN_TOKEN") or ""
    if not (base and token and rows):
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as c:
            for attempt in (1, 2):
                if not _RENDER_COOKIE:
                    lr = await c.post(f"{base}/inbox/login", data={"username": "", "password": token})
                    _RENDER_COOKIE = (lr.cookies.get("inbox_session") if lr.cookies is not None else None)
                    if not _RENDER_COOKIE:
                        print("[RENDER-SYNC] login to Render failed (is INBOX_ADMIN_TOKEN the same as on Render?)")
                        return False
                r = await c.post(f"{base}/api/inbox/admin/import-messages", json={"messages": [_forward_payload(x) for x in rows]},
                                 cookies={"inbox_session": _RENDER_COOKIE})
                if r.status_code == 401 and attempt == 1:
                    _RENDER_COOKIE = None            # session expired: log in again once
                    continue
                if r.status_code == 200:
                    return True
                print(f"[RENDER-SYNC] Render answered {r.status_code}; will retry later")
                return False
    except Exception as exc:
        print(f"[RENDER-SYNC] could not reach Render ({type(exc).__name__}); will retry later")
    return False


async def sync_pending_local() -> int:
    """Send every locally stored message that Render has not confirmed yet. Returns how many were confirmed."""
    if ENABLED:
        return 0
    rows = _unsynced_rows()
    if not rows or not await _forward_to_render(rows):
        return 0
    for r in rows:
        r["sync"] = "done"
    _save_local_outbound()
    return len(rows)


_ISO_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


async def import_message_rows(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Save already-sent messages (from a local server) into the messages table. Skips rows without a WhatsApp id
    (that is what makes re-sending harmless: ignore-duplicates on wamid)."""
    saved = skipped = 0
    for r in rows:
        e164, wamid, direction = _digits(str(r.get("e164") or "")), str(r.get("wamid") or "").strip(), r.get("direction")
        if not (ENABLED and e164 and wamid) or direction not in ("human", "ai", "system", "buyer"):
            skipped += 1
            continue
        payload = {k: v for k, v in {"sent_by": r.get("sent_by"), "filename": r.get("filename"), "media_id": r.get("media_id"),
                                     "mime": r.get("mime")}.items() if v}
        body: Dict[str, Any] = {"wamid": wamid, "sent_id_from_graph": wamid, "direction": direction, "e164": e164,
                                "text": str(r.get("text") or ""), "errored": bool(r.get("errored")), "error_detail": r.get("error_detail"),
                                "meta_statuses_jsonb": {}}
        if r.get("created_at") and _ISO_RE.match(str(r["created_at"])):
            body["created_at"] = str(r["created_at"])
        if payload:
            body["payload_jsonb"] = payload
        if r.get("media_type") in ("image", "document", "audio", "video"):
            body["media_type"] = r["media_type"]
        try:
            async with _client() as c:
                await c.post(f"{_REST_BASE}/messages", headers={**_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"}, json=body)
            saved += 1
        except Exception as exc:
            print(f"[supabase_client] import_message_rows failed for {wamid}: {type(exc).__name__}: {exc!s}")
            skipped += 1
    return {"saved": saved, "skipped": skipped}


async def insert_outbound_message(
    *,
    e164: str,
    direction: str,  # "ai" | "human" | "system"
    text: str,
    reply_to_wamid: Optional[str] = None,
    sent_id_from_graph: Optional[str] = None,
    errored: bool = False,
    error_detail: Optional[str] = None,
    meta_statuses: Optional[Dict[str, Any]] = None,
    sent_by: Optional[str] = None,
    media_type: Optional[str] = None,                 # "image" | "document" when a file was sent
    media_meta: Optional[Dict[str, Any]] = None,      # {filename, mime, size, media_id}
) -> None:
    if not ENABLED:
        _store_local_outbound(e164=e164, direction=direction, text=text, sent_id=sent_id_from_graph, errored=errored, error_detail=error_detail,
                              reply_to_wamid=reply_to_wamid, sent_by=sent_by, media_type=media_type, media_meta=media_meta)
        await sync_pending_local()           # hand it to Render (no-op unless RENDER_INBOX_URL is set)
        return
    if direction not in {"ai", "human", "system"}:
        direction = "system"
    body: Dict[str, Any] = {
        "direction": direction,
        "e164": e164,
        "text": text,
        "reply_to_wamid": reply_to_wamid,
        "sent_id_from_graph": sent_id_from_graph,
        "errored": errored,
        "error_detail": error_detail,
        "meta_statuses_jsonb": meta_statuses or {},
    }
    payload: Dict[str, Any] = dict(media_meta or {})
    if sent_by:
        payload["sent_by"] = sent_by                        # who on the team sent it
    if payload:
        body["payload_jsonb"] = payload
    if media_type:
        body["media_type"] = media_type
    if sent_id_from_graph:
        # Use Graph message-id as wamid for dedup uniqueness on send side.
        body["wamid"] = sent_id_from_graph
    async with _client() as c:
        await c.post(
            f"{_REST_BASE}/messages",
            headers={**_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=body,
        )


async def mark_status(wamid: str, recipient_e164: str, status_value: str, errors: Optional[str] = None) -> None:
    """Update meta_statuses_jsonb on outbound rows when Graph sends a status."""
    if not ENABLED:
        if wamid:
            local_mark_status(wamid, status_value, errors)
        return
    if not wamid:
        return
    patch = {
        "meta_statuses_jsonb": (
            f"jsonb_set(coalesce(meta_statuses_jsonb,'{{}}'::jsonb),"
            f"'{{{status_value}}}',to_jsonb(now()),true)"
        ),
    }
    if errors:
        patch["errored"] = True
        patch["error_detail"] = errors
    try:
        async with _client() as c:
            await c.patch(
                f"{_REST_BASE}/messages",
                headers={**_HEADERS, "Prefer": "return=minimal"},
                params={"wamid": f"eq.{wamid}", "e164": f"eq.{recipient_e164}"},
                json={"meta_statuses_jsonb": {"_op": "patch", **{status_value: True}}},  # fallback simple patch
            )
    except Exception:
        return


# ================================================================
# sessions — mirror of conversation_store (read NOT yet used by
# llm_assistant; written async as a mirror only in save_session)
# ================================================================

async def mirror_session(
    *,
    e164: str,
    inquiry_dict: Dict[str, Any],
    history_list: List[Dict[str, Any]],
    is_new_prospect: Optional[bool],
    lead_notified: bool,
    handoff_notified: bool,
    booking_intent_notified: bool,
    booking_link_shared_at_ts: Optional[float],
    followup_nudge_1_ts: Optional[float],
    followup_nudge_2_ts: Optional[float],
) -> None:
    if not (ENABLED and e164):
        return

    def _fmt(ts: Optional[float]) -> Optional[str]:
        if not ts:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))

    body: Dict[str, Any] = {
        "e164": e164,
        "inquiry_jsonb": inquiry_dict or {},
        "history_jsonb": history_list or [],
        "lead_notified": bool(lead_notified),
        "handoff_notified": bool(handoff_notified),
        "booking_intent_notified": bool(booking_intent_notified),
    }
    if is_new_prospect is not None:
        body["is_new_prospect"] = bool(is_new_prospect)
    bsa = _fmt(booking_link_shared_at_ts)
    if bsa is not None:
        body["booking_link_shared_at"] = bsa
    n1 = _fmt(followup_nudge_1_ts)
    if n1 is not None:
        body["followup_nudge_kind_1_at"] = n1
    n2 = _fmt(followup_nudge_2_ts)
    if n2 is not None:
        body["followup_nudge_kind_2_at"] = n2
    try:
        async with _client() as c:
            await c.post(
                f"{_REST_BASE}/sessions",
                headers={**_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"},
                json=body,
            )
    except Exception:
        return


# ================================================================
# chat list + thread READ (used by /api/inbox/* endpoints)
# ================================================================

async def list_chats(limit: int = 200) -> List[Dict[str, Any]]:
    if not ENABLED:
        return []
    async with _client() as c:
        r = await c.get(
            f"{_REST_BASE}/v_inbox_chat_list",
            headers={**_HEADERS},
            params={
                "order": "last_message_created_at.desc.nullslast",
                "limit": str(limit),
            },
        )
        if r.status_code >= 300:
            return []
        try:
            return r.json() or []
        except Exception:
            return []


async def thread_messages(e164: str, limit: int = 200) -> List[Dict[str, Any]]:
    if not (ENABLED and e164):
        return []
    async with _client() as c:
        r = await c.get(
            f"{_REST_BASE}/messages",
            headers={**_HEADERS},
            params={
                "e164": f"eq.{e164}",
                "order": "created_at.desc",
                "limit": str(limit),
                "select": "id,wamid,direction,e164,reply_to_wamid,text,media_type,media_url,created_at,sent_id_from_graph,errored,error_detail,meta_statuses_jsonb,payload_jsonb",
            },
        )
        if r.status_code >= 300:
            return []
        try:
            rows = r.json() or []
        except Exception:
            return []
    for r in rows:   # show who on the team wrote a human message
        pj = r.get("payload_jsonb") or {}
        r["held_by"] = pj.get("sent_by")
        if r.get("media_type"):
            blob = pj.get(r["media_type"]) if isinstance(pj.get(r["media_type"]), dict) else {}
            r["filename"] = r.get("filename") or pj.get("filename") or blob.get("filename")   # shown as the attachment name
            r["media_id"] = pj.get("media_id") or r.get("media_url") or blob.get("id")         # lets the thread show the file itself
            r["mime"] = pj.get("mime") or blob.get("mime_type")
            if r.get("text") in (media_placeholder(r["media_type"], r.get("filename")), f"📎 {r.get('filename')}"):
                r["text"] = ""                                 # the file itself is shown; no filler line above it
    rows.reverse()  # oldest first for the UI chat bubble stack
    return rows


# ================================================================
# claim mutex
# ================================================================

# Without Supabase (local development) chat locks live in this process, so two people using the same server see
# each other's lock. In production the inbox_claims table is the single source of truth and this is never used.
_LOCAL_CLAIMS: Dict[str, Dict[str, Any]] = {}


def _local_claim(e164: str) -> Optional[Dict[str, Any]]:
    row = _LOCAL_CLAIMS.get(e164)
    if row and row["expires_at"] <= time.time():
        _LOCAL_CLAIMS.pop(e164, None)
        return None
    return row


async def claim_is_human_held(e164: str) -> Optional[Dict[str, Any]]:
    """If held, return dict {held_by, session_id, expires_in_secs}; else None."""
    if not ENABLED:
        row = _local_claim(e164) if e164 else None
        return ({"held_by": row["held_by"], "session_id": row["session_id"], "expires_in_secs": max(0, int(row["expires_at"] - time.time()))}
                if row else None)
    if not e164:
        return None
    try:
        async with _client() as c:
            r = await c.get(
                f"{_REST_BASE}/inbox_claims",
                headers={**_HEADERS},
                params={
                    "e164": f"eq.{e164}",
                    "expires_at_ts": "gt.now()",
                    "limit": "1",
                },
            )
            rows = r.json() if r.status_code < 300 else []
            if not rows:
                return None
            row = rows[0]
            exp = row.get("expires_at_ts")
            secs: int = 0
            if exp:
                try:
                    import datetime as _dt
                    exp_dt = _dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    secs = max(0, int((exp_dt - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))
                except Exception:
                    secs = 120
            return {
                "held_by": row.get("held_by"),
                "session_id": row.get("session_id"),
                "expires_in_secs": secs,
            }
    except Exception:
        return None


async def claim_acquire(
    *, e164: str, held_by: str, session_id: str, ttl_seconds: int = 120
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Acquire or refresh a claim. Returns (True_acquired, current_holder_if_conflict).

    - Same (e164, held_by, session_id) → idempotent refresh → True.
    - Different holder with live claim → return (False, {held_by, ..., expires_in}).
    - Expired / no claim → overwrite → True.
    """
    if not ENABLED:
        if not (e164 and held_by and session_id):
            return True, None
        cur = _local_claim(e164)
        if cur and cur["session_id"] != session_id:
            return False, {"held_by": cur["held_by"], "session_id": cur["session_id"], "expires_in_secs": max(0, int(cur["expires_at"] - time.time()))}
        _LOCAL_CLAIMS[e164] = {"held_by": held_by, "session_id": session_id, "expires_at": time.time() + ttl_seconds}
        return True, None
    if not (e164 and held_by and session_id):
        return True, None
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_seconds))
    heartbeat = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    try:
        async with _client() as c:
            # Try upsert-if-expired-or-mine:
            existing = await claim_is_human_held(e164)
            if existing and existing["session_id"] != session_id:
                # Someone else holds it.
                return False, existing
            body = {
                "e164": e164,
                "held_by": held_by,
                "session_id": session_id,
                "expires_at_ts": expires,
                "heartbeat_ts": heartbeat,
            }
            await c.post(
                f"{_REST_BASE}/inbox_claims",
                headers={**_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"},
                json=body,
            )
            # Audit trail
            await _audit(
                actor=f"human:{held_by}",
                action="claim_acquired",
                e164=e164,
                detail={"session_id": session_id, "ttl_seconds": ttl_seconds},
            )
            return True, None
    except Exception:
        return True, None


async def claim_release(*, e164: str, held_by: Optional[str], session_id: str) -> bool:
    if not ENABLED:
        cur = _local_claim(e164) if e164 else None
        if cur and cur["session_id"] == session_id:
            _LOCAL_CLAIMS.pop(e164, None)
        return True
    if not e164:
        return True
    try:
        async with _client() as c:
            params: Dict[str, str] = {"e164": f"eq.{e164}", "session_id": f"eq.{session_id}"}
            if held_by:
                params["held_by"] = f"eq.{held_by}"
            await c.delete(f"{_REST_BASE}/inbox_claims", headers={**_HEADERS}, params=params)
            await _audit(
                actor=f"human:{held_by or 'system'}",
                action="claim_released",
                e164=e164,
                detail={"session_id": session_id},
            )
        return True
    except Exception:
        return True


# ================================================================
# quiet-hours scheduling / outbound_schedules table
# ================================================================

def local_last_buyer_ts(e164: str) -> Optional[float]:
    """Newest buyer message of a chat in the local (no-Supabase) store, as a unix timestamp."""
    import calendar
    best: Optional[float] = None
    for m in _LOCAL_OUTBOUND.get(_digits(e164), []):
        if m.get("direction") != "buyer":
            continue
        try:
            t = float(calendar.timegm(time.strptime(str(m["created_at"])[:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:
            continue
        best = t if best is None else max(best, t)
    return best


def _iso_to_unix(value: Any) -> Optional[float]:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


async def get_last_buyer_message_at(e164: str) -> Optional[float]:
    """Unix time of the buyer's newest message for this number (WhatsApp's 24h window runs from it), or None.
    Accepts "+60..." or "60..."; reads the session row and falls back to the messages table itself."""
    digits = _digits(e164)
    if not digits:
        return None
    if not ENABLED:
        return local_last_buyer_ts(digits)
    flt = f'in.("{digits}","+{digits}")'
    try:
        async with _client() as c:
            r = await c.get(f"{_REST_BASE}/sessions", headers={**_HEADERS}, params={"e164": flt, "select": "last_buyer_message_at", "limit": "1"})
            if r.status_code < 300:
                rows = r.json() or []
                ts = _iso_to_unix(rows[0].get("last_buyer_message_at")) if rows and rows[0].get("last_buyer_message_at") else None
                if ts is not None:
                    return ts
            r = await c.get(f"{_REST_BASE}/messages", headers={**_HEADERS},
                            params={"e164": flt, "direction": "eq.buyer", "order": "created_at.desc", "limit": "1", "select": "created_at"})
            if r.status_code < 300:
                rows = r.json() or []
                return _iso_to_unix(rows[0].get("created_at")) if rows else None
    except Exception:
        return None
    return None


async def insert_outbound_schedule(row: Dict[str, Any]) -> None:
    """Insert one row into outbound_schedules. SAFE NO-OP if !ENABLED.
    Expected keys: e164, direction (schedule_direction enum string),
    scheduled_for (ISO timestamptz string). Optional: template_name,
    template_params, plain_text, sender_direction, created_by, claim_session_id."""
    if not ENABLED:
        return
    if not row:
        return
    body: Dict[str, Any] = dict(row)
    try:
        async with _client() as c:
            await c.post(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS, "Prefer": "return=minimal"},
                json=body,
            )
    except Exception:
        return


async def claim_next_pending_scheduled_batch(
    now_utc_ts: float, limit: int = 200
) -> List[Dict[str, Any]]:
    """V1 client-level claim pattern for outbound_schedules.

    Steps:
      1. SELECT id FROM outbound_schedules WHERE status='pending'
         AND scheduled_for <= now_utc_ts_iso ORDER BY scheduled_for LIMIT limit.
      2. For each id individually UPDATE status='claimed_temp', sent_at=now()
         WHERE id=$id AND status='pending' (conditional WHERE prevents
         double-claim on races).
      3. Re-SELECT the full rows for ids that successfully transitioned.

    V1 is sufficient for <<500 schedules/day scale (overlap is rare).
    V2 TODO: replace with a Postgres RPC using FOR UPDATE SKIP LOCKED.

    Returns list of full claimed row dicts (may be empty if nothing ready).
    SAFE: returns [] if Supabase disabled / error."""
    if not ENABLED:
        return []
    try:
        import datetime as _dt_local
        now_iso = _dt_local.datetime.fromtimestamp(now_utc_ts, tz=_dt_local.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return []
    claimed_ids: List[int] = []
    try:
        async with _client() as c:
            # --- Step 1: SELECT candidate ids ---
            r1 = await c.get(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS},
                params={
                    "status": "eq.pending",
                    "scheduled_for": f"lte.{now_iso}",
                    "order": "scheduled_for.asc",
                    "limit": str(limit),
                    "select": "id",
                },
            )
            if r1.status_code >= 300:
                return []
            candidates = r1.json() or []
            candidate_ids: List[int] = []
            for row in candidates:
                try:
                    candidate_ids.append(int(row["id"]))
                except Exception:
                    continue
            if not candidate_ids:
                return []
            # --- Step 2: UPDATE each id individually with conditional WHERE ---
            for cid in candidate_ids:
                try:
                    r2 = await c.patch(
                        f"{_REST_BASE}/outbound_schedules",
                        headers={
                            **_HEADERS,
                            "Prefer": "return=representation",
                        },
                        params={"id": f"eq.{cid}", "status": "eq.pending"},
                        json={"status": "claimed_temp", "sent_at": "now()"},
                    )
                    if r2.status_code < 300:
                        updated = r2.json() or []
                        if updated:
                            claimed_ids.append(cid)
                except Exception:
                    continue
            if not claimed_ids:
                return []
            # --- Step 3: Re-SELECT full rows for the successful ids ---
            id_list = ",".join(str(x) for x in claimed_ids)
            r3 = await c.get(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS},
                params={"id": f"in.({id_list})"},
            )
            if r3.status_code >= 300:
                return []
            rows = r3.json() or []
            return list(rows)
    except Exception:
        return []


async def update_schedule_status(
    id: int,
    status: str,
    *,
    error_detail: Optional[str] = None,
    sent_at_utc: Optional[float] = None,
) -> None:
    """PATCH outbound_schedules row. SAFE NO-OP if Supabase disabled."""
    if not (ENABLED and id is not None and status):
        return
    body: Dict[str, Any] = {"status": status}
    if error_detail:
        body["error_detail"] = error_detail
    if sent_at_utc:
        try:
            import datetime as _dt_local2
            body["sent_at"] = _dt_local2.datetime.fromtimestamp(
                sent_at_utc, tz=_dt_local2.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    try:
        async with _client() as c:
            await c.patch(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS, "Prefer": "return=minimal"},
                params={"id": f"eq.{int(id)}"},
                json=body,
            )
    except Exception:
        return


# ================================================================
# Admin ops console read-only helpers (new for /inbox/admin dashboard)
# All are no-ops with safe returns when ENABLED=False; never raise.
# ================================================================

async def admin_list_active_inbox_claims(limit: int = 50) -> List[Dict[str, Any]]:
    """Currently held inbox claims (released_at IS NULL), most recent first."""
    if not ENABLED:
        return []
    try:
        async with _client() as c:
            r = await c.get(
                f"{_REST_BASE}/inbox_claims",
                headers={**_HEADERS},
                params={
                    "released_at": "is.null",
                    "order": "acquired_at.desc",
                    "limit": str(max(int(limit), 1)),
                    "select": "id,e164,held_by,session_id,acquired_at,last_heartbeat_at,expires_at",
                },
            )
            if r.status_code == 200:
                return list(r.json() or [])
    except Exception:
        return []
    return []


async def admin_outbound_schedules_status_counts() -> Dict[str, int]:
    """Count of outbound_schedules rows grouped by status enum value.

    Postgres doesn't have a trivial aggregate via Supabase REST for arbitrary
    group-by, so we issue one lightweight count=exact HEAD per known status.
    V1 enum values per migration 0002: pending, claimed_temp, sent, failed,
    cancelled.  Unknowns counted under "_unknown_rows_total"."""
    if not ENABLED:
        return {}
    known = ["pending", "claimed_temp", "sent", "failed", "cancelled"]
    counts: Dict[str, int] = {s: 0 for s in known}
    unknown = 0
    try:
        async with _client() as c:
            for st in known:
                r = await c.get(
                    f"{_REST_BASE}/outbound_schedules",
                    headers={**_HEADERS, "Prefer": "count=exact,head=true"},
                    params={"status": f"eq.{st}"},
                )
                try:
                    counts[st] = int(r.headers.get("Content-Range", "*/0").split("/")[-1])
                except Exception:
                    counts[st] = 0
            # total rows to compute _unknown:
            rt = await c.get(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS, "Prefer": "count=exact,head=true"},
            )
            try:
                total = int(rt.headers.get("Content-Range", "*/0").split("/")[-1])
                unknown = max(0, total - sum(counts.values()))
            except Exception:
                unknown = 0
    except Exception:
        return counts
    if unknown:
        counts["_unknown_rows_total"] = unknown
    return counts


async def admin_outbound_schedules_pending_sample(limit: int = 20) -> List[Dict[str, Any]]:
    """Next upcoming outbound_schedules (status in pending/claimed_temp), soonest first."""
    if not ENABLED:
        return []
    try:
        async with _client() as c:
            r = await c.get(
                f"{_REST_BASE}/outbound_schedules",
                headers={**_HEADERS},
                params={
                    "status": "in.(pending,claimed_temp)",
                    "order": "scheduled_for.asc",
                    "limit": str(max(int(limit), 1)),
                    "select": "id,e164,direction,template_name,plain_text,sender_direction,created_by,scheduled_for,status,claim_session_id,created_at",
                },
            )
            if r.status_code == 200:
                rows = list(r.json() or [])
                return rows
    except Exception:
        return []
    return []


async def admin_sessions_count_db() -> int:
    """Count Supabase sessions rows (independent of Redis). Returns 0 if disabled."""
    if not ENABLED:
        return 0
    try:
        async with _client() as c:
            r = await c.get(
                f"{_REST_BASE}/sessions",
                headers={**_HEADERS, "Prefer": "count=exact,head=true"},
            )
            try:
                return int(r.headers.get("Content-Range", "*/0").split("/")[-1])
            except Exception:
                return 0
    except Exception:
        return 0


# ================================================================
# audit log
# ================================================================

_AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".audit_log.jsonl")


def _audit_file_append(actor: str, action: str, e164: Optional[str], detail: Optional[Dict[str, Any]]) -> None:
    """Dev fallback (no Supabase): keep the log in a local JSONL file."""
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": actor, "action": action,
               "e164": e164, "detail_jsonb": detail or {}}
        with open(_AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _audit(actor: str, action: str, *, e164: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> None:
    if not ENABLED:
        _audit_file_append(actor, action, e164, detail)
        return
    try:
        async with _client() as c:
            await c.post(
                f"{_REST_BASE}/audit_events",
                headers={**_HEADERS, "Prefer": "return=minimal"},
                json={
                    "actor": actor,
                    "action": action,
                    "e164": e164,
                    "detail_jsonb": detail or {},
                },
            )
    except Exception:
        return


async def audit(actor: str, action: str, *, e164: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> None:
    """Public alias so callers outside this file can audit things."""
    await _audit(actor=actor, action=action, e164=e164, detail=detail)


async def list_audit(limit: int = 200, action: Optional[str] = None, q: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest-first audit events (Supabase, or the local JSONL file in dev)."""
    limit = max(1, min(1000, int(limit)))
    rows: List[Dict[str, Any]] = []
    if ENABLED:
        params: Dict[str, str] = {"order": "ts.desc", "limit": str(limit)}
        if action:
            params["action"] = f"eq.{action}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
                r = await c.get(f"{_REST_BASE}/audit_events", headers={**_HEADERS}, params=params)
            rows = r.json() if r.status_code < 300 else []
        except Exception:
            rows = []
    else:
        try:
            with open(_AUDIT_FILE, "r", encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
        except Exception:
            rows = []
        rows.reverse()
        if action:
            rows = [r for r in rows if r.get("action") == action]
        rows = rows[:limit]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in json.dumps(r, ensure_ascii=False).lower()]
    return rows


# ================================================================
# import / delete a whole chat (admin)
# ================================================================

_LONG_TIMEOUT = httpx.Timeout(20.0, connect=3.0)


async def count_messages(e164: str) -> int:
    if not ENABLED:
        return 0
    try:
        async with httpx.AsyncClient(timeout=_LONG_TIMEOUT) as c:
            r = await c.get(f"{_REST_BASE}/messages", headers={**_HEADERS, "Prefer": "count=exact"},
                            params={"e164": f"eq.{e164}", "select": "id", "limit": "1"})
        return int((r.headers.get("content-range") or "*/0").split("/")[-1] or 0)
    except Exception:
        return 0


async def import_messages(e164: str, rows: List[Dict[str, Any]]) -> int:
    """Bulk-insert already-built message rows for one chat. Returns rows sent (0 on failure)."""
    if not (ENABLED and rows):
        return 0
    try:
        async with httpx.AsyncClient(timeout=_LONG_TIMEOUT) as c:
            r = await c.post(f"{_REST_BASE}/messages", headers={**_HEADERS, "Prefer": "return=minimal"}, json=rows)
        return len(rows) if r.status_code < 300 else 0
    except Exception:
        return 0


async def delete_chat(e164_variants: List[str]) -> Dict[str, int]:
    """Delete a chat everywhere in Supabase: messages, claim, session row."""
    out = {"messages": 0, "claims": 0, "sessions": 0}
    for v in e164_variants:
        _LOCAL_CLAIMS.pop(v, None)
    _forget_local_outbound(list(e164_variants))
    if not (ENABLED and e164_variants):
        return out
    flt = "in.(" + ",".join('"%s"' % v.replace('"', "") for v in e164_variants) + ")"
    hdr = {**_HEADERS, "Prefer": "return=minimal,count=exact"}
    async with httpx.AsyncClient(timeout=_LONG_TIMEOUT) as c:
        try:  # nothing queued for this buyer may be sent after the chat is gone
            await c.patch(f"{_REST_BASE}/outbound_schedules", headers={**_HEADERS, "Prefer": "return=minimal"},
                          params={"e164": flt, "status": "in.(pending,claimed_temp)"}, json={"status": "cancelled"})
        except Exception as exc:
            print(f"[supabase_client] delete_chat cancel schedules failed: {type(exc).__name__}: {exc!s}")
        for table, key in (("messages", "messages"), ("inbox_claims", "claims"), ("sessions", "sessions")):
            try:
                r = await c.delete(f"{_REST_BASE}/{table}", headers=hdr, params={"e164": flt})
                if r.status_code < 300:
                    out[key] = int((r.headers.get("content-range") or "*/0").split("/")[-1] or 0)
            except Exception as exc:
                print(f"[supabase_client] delete_chat {table} failed: {type(exc).__name__}: {exc!s}")
    return out


async def reset_session_mirror(e164_variants: List[str]) -> bool:
    """After "forget AI memory": blank the mirrored session summary (inquiry chips, history, flags).
    Messages are left untouched so the thread stays readable in the inbox."""
    if not (ENABLED and e164_variants):
        return False
    flt = "in.(" + ",".join('"%s"' % v.replace('"', "") for v in e164_variants) + ")"
    body = {"inquiry_jsonb": {}, "history_jsonb": [], "lead_notified": False, "handoff_notified": False,
            "booking_intent_notified": False, "booking_link_shared_at": None,
            "followup_nudge_kind_1_at": None, "followup_nudge_kind_2_at": None}
    try:
        async with httpx.AsyncClient(timeout=_LONG_TIMEOUT) as c:
            r = await c.patch(f"{_REST_BASE}/sessions", headers={**_HEADERS, "Prefer": "return=minimal"}, params={"e164": flt}, json=body)
        return r.status_code < 300
    except Exception as exc:
        print(f"[supabase_client] reset_session_mirror failed: {type(exc).__name__}: {exc!s}")
        return False


async def claim_force_release(e164: str) -> bool:
    """Admin override: clear whoever holds this chat."""
    if not ENABLED:
        _LOCAL_CLAIMS.pop(e164, None)
        return True
    if not e164:
        return True
    try:
        async with _client() as c:
            await c.delete(f"{_REST_BASE}/inbox_claims", headers={**_HEADERS}, params={"e164": f"eq.{e164}"})
        return True
    except Exception:
        return False
