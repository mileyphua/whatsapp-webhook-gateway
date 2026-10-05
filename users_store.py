"""Team members the admin adds so they can reply to buyers from the inbox.

Stored in Upstash Redis (hash `inbox_users`) when configured, else a local JSON file (dev). Passwords are salted
PBKDF2-SHA256 hashes; clear-text passwords are never stored or returned. Usernames are lower-case, 3-32 chars.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

_REDIS_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""
_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".inbox_users.json")
_HASH = "inbox_users"
_lock = threading.Lock()
ITERATIONS = 200_000
MIN_PASSWORD = 8
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
RESERVED = {"admin", "root", "system"}


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def _redis_on() -> bool:
    return bool(_REDIS_URL and _REDIS_TOKEN)


def _hdr() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_REDIS_TOKEN}"}


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex()


def verify_password(user: Optional[Dict[str, Any]], password: str) -> bool:
    """Constant-time check. Disabled users never verify."""
    if not user or user.get("disabled") or not password:
        return False
    try:
        expected = user["pw_hash"]
        return hmac.compare_digest(_hash(password, bytes.fromhex(user["salt"])), expected)
    except Exception:
        return False


async def _all() -> Dict[str, Dict[str, Any]]:
    if _redis_on():
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{_REDIS_URL}/hgetall/{_HASH}", headers=_hdr())
        r.raise_for_status()
        flat = r.json().get("result") or []
        return {flat[i]: json.loads(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
    with _lock:
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}


async def _put(username: str, user: Dict[str, Any]) -> None:
    if _redis_on():
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{_REDIS_URL}/hset/{_HASH}/{username}", headers=_hdr(), content=json.dumps(user, ensure_ascii=False))
        r.raise_for_status()
        return
    with _lock:
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        data[username] = user
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)


async def get_user(username: str) -> Optional[Dict[str, Any]]:
    return (await _all()).get(normalize_username(username))


async def list_users() -> List[Dict[str, Any]]:
    """Public view: never includes the hash or salt."""
    users = (await _all()).values()
    return sorted(({"username": u["username"], "name": u["name"], "disabled": bool(u.get("disabled")),
                    "created_ts": u.get("created_ts")} for u in users), key=lambda u: u["username"])


async def create_user(*, username: str, name: str, password: str) -> Dict[str, Any]:
    uname = normalize_username(username)
    if not _USERNAME.match(uname) or uname in RESERVED:
        raise ValueError("Username must be 3-32 letters, numbers, dots, dashes or underscores (and not 'admin').")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
    if uname in await _all():
        raise ValueError("That username already exists.")
    salt = secrets.token_bytes(16)
    user = {"username": uname, "name": (name or "").strip()[:60] or uname, "salt": salt.hex(), "pw_hash": _hash(password, salt),
            "disabled": False, "created_ts": time.time()}
    await _put(uname, user)
    return user


async def update_user(username: str, *, name: Optional[str] = None, password: Optional[str] = None,
                      disabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    user = await get_user(username)
    if not user:
        return None
    if name is not None:
        user["name"] = name.strip()[:60] or user["username"]
    if password is not None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
        salt = secrets.token_bytes(16)
        user["salt"], user["pw_hash"] = salt.hex(), _hash(password, salt)
    if disabled is not None:
        user["disabled"] = bool(disabled)
    await _put(user["username"], user)
    return user


async def delete_user(username: str) -> None:
    uname = normalize_username(username)
    if _redis_on():
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{_REDIS_URL}/hdel/{_HASH}/{uname}", headers=_hdr())
        r.raise_for_status()
        return
    with _lock:
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        data.pop(uname, None)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
