"""Storage for reply feedback and the lessons distilled from it.

Backends: Upstash Redis hashes when configured (survives redeploys), else a
local JSON file (dev). Best-effort: reads return empty on failure and never
raise into the reply path.

  ai_feedback : id -> {id, ts, e164, ai_text, buyer_text, rating, tags, note,
                       better_reply, actor, processed}
  ai_lessons  : id -> {id, ts, text, kind, status, source_ids}
                kind   = style | behavior | knowledge
                status = pending | active | disabled
"""
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

_REDIS_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ai_learning.json")
_lock = threading.Lock()

FEEDBACK = "ai_feedback"
LESSONS = "ai_lessons"
MAX_TEXT = 2000


def _redis_on() -> bool:
    return bool(_REDIS_URL and _REDIS_TOKEN)


def _hdr() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_REDIS_TOKEN}"}


def _file_read() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _file_write(data: Dict[str, Dict[str, Any]]) -> None:
    with open(_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


async def _hgetall(name: str) -> Dict[str, Dict[str, Any]]:
    if _redis_on():
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{_REDIS_URL}/hgetall/{name}", headers=_hdr())
            r.raise_for_status()
            flat = r.json().get("result") or []
            return {flat[i]: json.loads(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
        except Exception as exc:
            print(f"[feedback_store] hgetall {name} failed: {type(exc).__name__}: {exc!s}")
            return {}
    with _lock:
        return _file_read().get(name, {})


async def _hset(name: str, key: str, value: Dict[str, Any]) -> None:
    if _redis_on():
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{_REDIS_URL}/hset/{name}/{key}", headers=_hdr(), content=json.dumps(value, ensure_ascii=False))
        r.raise_for_status()
        return
    with _lock:
        data = _file_read()
        data.setdefault(name, {})[key] = value
        _file_write(data)


async def _hdel(name: str, key: str) -> None:
    if _redis_on():
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{_REDIS_URL}/hdel/{name}/{key}", headers=_hdr())
        r.raise_for_status()
        return
    with _lock:
        data = _file_read()
        data.get(name, {}).pop(key, None)
        _file_write(data)


# ---------------------------------------------------------------- feedback

async def add_feedback(*, e164: str, ai_text: str, buyer_text: str, rating: str,
                       tags: Optional[List[str]] = None, note: str = "",
                       better_reply: str = "", actor: str = "") -> Dict[str, Any]:
    """Save (or replace) the rating for one AI reply. One rating per (chat, reply text)."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    ai_text = (ai_text or "").strip()[:MAX_TEXT]
    if not ai_text:
        raise ValueError("ai_text required")
    # deterministic id => re-rating the same reply overwrites instead of duplicating
    fid = uuid.uuid5(uuid.NAMESPACE_URL, f"{''.join(ch for ch in e164 if ch.isdigit())}|{ai_text}").hex[:16]
    item = {
        "id": fid, "ts": time.time(), "e164": e164, "ai_text": ai_text,
        "buyer_text": (buyer_text or "").strip()[:MAX_TEXT], "rating": rating,
        "tags": [str(t)[:40] for t in (tags or [])][:8], "note": (note or "").strip()[:MAX_TEXT],
        "better_reply": (better_reply or "").strip()[:MAX_TEXT], "actor": actor, "processed": False,
    }
    await _hset(FEEDBACK, fid, item)
    return item


async def list_feedback(e164: Optional[str] = None) -> List[Dict[str, Any]]:
    items = list((await _hgetall(FEEDBACK)).values())
    if e164:
        key = "".join(ch for ch in e164 if ch.isdigit())
        items = [i for i in items if "".join(ch for ch in i.get("e164", "") if ch.isdigit()) == key]
    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return items


async def mark_processed(ids: List[str]) -> None:
    allf = await _hgetall(FEEDBACK)
    for i in ids:
        if i in allf:
            allf[i]["processed"] = True
            await _hset(FEEDBACK, i, allf[i])


# ----------------------------------------------------------------- lessons

async def add_lessons(lessons: List[Dict[str, Any]], source_ids: List[str]) -> List[Dict[str, Any]]:
    out = []
    for l in lessons:
        text = str(l.get("text") or "").strip()[:400]
        if not text:
            continue
        kind = l.get("kind") if l.get("kind") in ("style", "behavior", "knowledge") else "behavior"
        lid = uuid.uuid4().hex[:12]
        item = {"id": lid, "ts": time.time(), "text": text, "kind": kind, "status": "pending", "source_ids": source_ids[:20]}
        await _hset(LESSONS, lid, item)
        out.append(item)
    return out


async def list_lessons(status: Optional[str] = None) -> List[Dict[str, Any]]:
    items = list((await _hgetall(LESSONS)).values())
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return items


async def set_lesson_status(lesson_id: str, status: str) -> Optional[Dict[str, Any]]:
    if status not in ("pending", "active", "disabled"):
        raise ValueError("bad status")
    allf = await _hgetall(LESSONS)
    item = allf.get(lesson_id)
    if not item:
        return None
    item["status"] = status
    await _hset(LESSONS, lesson_id, item)
    return item


async def delete_lesson(lesson_id: str) -> None:
    await _hdel(LESSONS, lesson_id)
