"""Human-assigned display names for WhatsApp numbers (inbox reference labels).

Storage: Upstash Redis hash `contact_names` when configured (survives redeploys),
otherwise a local JSON file (dev). Keys are digits-only numbers so "+60123" and
"60123" are the same contact. Best-effort: never raises.
"""
import json
import os
from typing import Dict

import httpx

_REDIS_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
_HASH = "contact_names"
_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".contact_names.json")
MAX_LEN = 60


def normalize(e164: str) -> str:
    return "".join(ch for ch in (e164 or "") if ch.isdigit())


def _redis_on() -> bool:
    return bool(_REDIS_URL and _REDIS_TOKEN)


def _file_load() -> Dict[str, str]:
    try:
        with open(_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


async def get_all() -> Dict[str, str]:
    if _redis_on():
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{_REDIS_URL}/hgetall/{_HASH}", headers={"Authorization": f"Bearer {_REDIS_TOKEN}"})
            r.raise_for_status()
            flat = r.json().get("result") or []
            return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        except Exception as exc:
            print(f"[contact_names] redis load failed: {type(exc).__name__}: {exc!s}")
            return {}
    return _file_load()


async def set_name(e164: str, name: str) -> str:
    """Set (or clear, if blank) the name. Returns the stored name."""
    key = normalize(e164)
    name = (name or "").strip()[:MAX_LEN]
    if not key:
        raise ValueError("invalid number")
    if _redis_on():
        hdr = {"Authorization": f"Bearer {_REDIS_TOKEN}"}
        async with httpx.AsyncClient(timeout=8.0) as c:
            if name:
                r = await c.post(f"{_REDIS_URL}/hset/{_HASH}/{key}", headers=hdr, content=name)
            else:
                r = await c.post(f"{_REDIS_URL}/hdel/{_HASH}/{key}", headers=hdr)
        r.raise_for_status()
        return name
    data = _file_load()
    if name:
        data[key] = name
    else:
        data.pop(key, None)
    with open(_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return name
