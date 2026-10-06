"""Is the AI model answering? Tracks the outcome of the latest model calls so the inbox can tell people (top banner,
system note in the chat, team email) as soon as replies start falling back, e.g. when OpenRouter credits run out."""
import threading
import time
from typing import Any, Dict, Optional

CREDITS_URL = "https://openrouter.ai/settings/credits"
_lock = threading.Lock()
_state: Dict[str, Any] = {}
_emailed: Dict[str, float] = {}

_TEXT = {
    "credits": ("AI replies are offline: OpenRouter credits are used up",
                f"Top up at {CREDITS_URL}. Until then buyers get a short fallback message and price questions are routed to a human."),
    "auth": ("AI replies are offline: the OpenRouter key was rejected",
             "Check OPENROUTER_API_KEY in the Render environment. Until then buyers get a short fallback message."),
    "missing_key": ("AI replies are offline: no OpenRouter key is set",
                    "Set OPENROUTER_API_KEY in the Render environment. Until then buyers get a short fallback message."),
    "rate_limit": ("AI is being rate-limited by OpenRouter",
                   "This usually clears by itself. If it keeps happening, check your OpenRouter limits."),
    "error": ("AI replies are failing",
              "Check the Render logs for lines starting with [llm]. Buyers get a short fallback message meanwhile."),
}


def reset() -> None:
    with _lock:
        _state.clear()
        _state.update({"ok": True, "kind": None, "status": None, "message": "", "since": None, "last_failure": None, "failures": 0, "last_ok": None})
        _emailed.clear()


reset()


def classify(exc: BaseException) -> Dict[str, Any]:
    status = getattr(exc, "status_code", None)
    msg = str(exc)
    low = msg.lower()
    if status == 402 or "insufficient credits" in low or "requires more credits" in low:
        kind = "credits"
    elif status in (401, 403) or "invalid api key" in low or "no auth credentials" in low:
        kind = "auth"
    elif status == 429 or "rate limit" in low:
        kind = "rate_limit"
    else:
        kind = "error"
    return {"kind": kind, "status": status, "message": msg[:300]}


def record_failure(exc: Optional[BaseException] = None, *, kind: Optional[str] = None, message: str = "") -> None:
    info = classify(exc) if exc is not None else {"kind": kind or "error", "status": None, "message": message[:300]}
    if kind:
        info["kind"] = kind
    with _lock:
        if _state.get("ok", True) or _state.get("kind") != info["kind"]:
            _state["since"] = time.time()
        _state.update(ok=False, kind=info["kind"], status=info["status"], message=info["message"], last_failure=time.time(),
                      failures=_state.get("failures", 0) + 1)


def record_success() -> None:
    with _lock:
        _state.update(ok=True, kind=None, status=None, message="", since=None, failures=0, last_ok=time.time())


def current() -> Dict[str, Any]:
    with _lock:
        s = dict(_state)
    kind = s.get("kind")
    title, help_text = _TEXT.get(kind or "", ("", ""))
    return {**s, "title": title, "help": help_text, "help_url": CREDITS_URL if kind == "credits" else ""}


def should_email(kind: str, every_seconds: float = 3600.0) -> bool:
    """True at most once per `every_seconds` for each kind of problem (one alert, not one per buyer)."""
    now = time.time()
    with _lock:
        if now - _emailed.get(kind, 0.0) < every_seconds:
            return False
        _emailed[kind] = now
        return True
