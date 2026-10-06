"""Which AI model is the app using, and what can it do? Shown on the Ops page for the admin:
the model name, what it accepts as input (text / image / file), the parameters it supports, whether the app's own settings
are compatible, every change of model, and live tests (text, image, document). Facts come from OpenRouter's public model list."""
import asyncio
import base64
import os
import struct
import time
import zlib
from typing import Any, Dict, List, Optional

import httpx

import ai_health
import feedback_store as fs
import supabase_client as _sb

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_SECONDS = 1800.0
HISTORY = "ai_model_history"

# what the app sends on its calls to the model (llm_assistant._single_turn_chat and learning._llm_json)
APP_PARAMETERS = [
    {"name": "temperature", "value": "0.15 (answers) / 0.1 (planner)", "needs": ["temperature"]},
    {"name": "max_tokens", "value": "900 (answers) / 400-1400 (planner)", "needs": ["max_tokens", "max_completion_tokens"]},
    {"name": "tools", "value": "capture inquiry, handoff, booking link, web search", "needs": ["tools"]},
    {"name": "tool_choice", "value": "auto", "needs": ["tool_choice"]},
    {"name": "reasoning.effort", "value": "low", "needs": ["reasoning", "reasoning_effort"]},
]

_cache: Dict[str, Any] = {"ts": 0.0, "models": None, "error": ""}
_last_noted: Optional[str] = None


def reset() -> None:
    global _last_noted
    _cache.update(ts=0.0, models=None, error="")
    _last_noted = None


def current_model_id() -> str:
    import llm_assistant                      # lazy: avoids a circular import at start-up
    return os.getenv("OPENROUTER_MODEL") or llm_assistant.OPENROUTER_MODEL_DEFAULT


def _client():
    import llm_assistant
    return llm_assistant._openrouter_client()


async def _models(force: bool = False) -> Optional[List[Dict[str, Any]]]:
    if not force and _cache["models"] is not None and time.time() - _cache["ts"] < CACHE_SECONDS:
        return _cache["models"]
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "petrobind-inbox"}) as c:
            r = await c.get(MODELS_URL)
        data = r.json().get("data") or []
        _cache.update(ts=time.time(), models=data, error="")
    except Exception as exc:
        _cache["error"] = f"Could not reach OpenRouter: {exc}"
        if _cache["models"] is None:
            return None
        return _cache["models"]          # keep the last known list (the caller marks it stale)
    return _cache["models"]


def _lookup(models: Optional[List[Dict[str, Any]]], model_id: str) -> Optional[Dict[str, Any]]:
    return next((m for m in (models or []) if m.get("id") == model_id or m.get("canonical_slug") == model_id), None)


def _per_million(v: Any) -> Optional[float]:
    try:
        return round(float(v) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _shape(model_id: str, m: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    arch = (m or {}).get("architecture") or {}
    inputs = list(arch.get("input_modalities") or [])
    supported = sorted((m or {}).get("supported_parameters") or [])
    pricing = (m or {}).get("pricing") or {}
    top = (m or {}).get("top_provider") or {}
    params = []
    for p in APP_PARAMETERS:
        ok = any(n in supported for n in p["needs"])
        params.append({"name": p["name"], "value": p["value"], "supported": ok,
                       "note": "" if ok else "Not supported by this model, so it is ignored by the provider."})
    return {
        "model": model_id, "found": m is not None, "name": (m or {}).get("name", model_id), "description": (m or {}).get("description", ""),
        "context_length": (m or {}).get("context_length"), "max_completion_tokens": top.get("max_completion_tokens"),
        "moderated": bool(top.get("is_moderated")), "knowledge_cutoff": (m or {}).get("knowledge_cutoff"),
        "input_modalities": inputs, "output_modalities": list(arch.get("output_modalities") or []),
        "supported_parameters": supported, "default_parameters": (m or {}).get("default_parameters") or {},
        "pricing": {"input_per_million": _per_million(pricing.get("prompt")), "output_per_million": _per_million(pricing.get("completion")),
                    "image_per_thousand": _per_million(pricing.get("image")) if pricing.get("image") else None},
        "capabilities": {"text": "text" in inputs, "images": "image" in inputs, "documents": "file" in inputs, "tools": "tools" in supported,
                         "reasoning": "reasoning" in supported or "reasoning_effort" in supported,
                         "structured_outputs": "structured_outputs" in supported},
        "app_parameters": params,
    }


async def describe(force: bool = False) -> Dict[str, Any]:
    model_id = current_model_id()
    models = await _models(force)
    out = _shape(model_id, _lookup(models, model_id))
    err = _cache["error"]
    out.update(source=MODELS_URL, fetched_at=_cache["ts"] or None, stale=bool(err and models is not None), error=err)
    if models is not None and not out["found"]:
        out["error"] = f"Model '{model_id}' is not listed by OpenRouter (typo in OPENROUTER_MODEL, or the model was retired)."
    return out


async def can_read(kind: str) -> bool:
    """Can the current model take this kind of buyer attachment as input? Unknown counts as yes (we try, and fall back if it fails)."""
    d = await describe()
    if not d["found"]:
        return True
    return d["capabilities"]["images" if kind == "image" else "documents"]


# ---------------------------------------------------------------- history of model changes
async def history(limit: int = 10) -> List[Dict[str, Any]]:
    rows = list((await fs._hgetall(HISTORY)).values())
    rows.sort(key=lambda r: r.get("at", 0), reverse=True)
    return rows[:limit]


async def record_change_if_needed(model_id: str) -> Optional[Dict[str, Any]]:
    """Remember the first model we see and every later change (with what that model can take as input)."""
    rows = await history(1)
    if rows and rows[0].get("model") == model_id:
        return None
    shaped = _shape(model_id, _lookup(await _models(), model_id))
    rec = {"model": model_id, "from": rows[0]["model"] if rows else "", "at": time.time(), "input_modalities": shaped["input_modalities"],
           "output_modalities": shaped["output_modalities"], "supported_parameters": shaped["supported_parameters"],
           "context_length": shaped["context_length"], "found": shaped["found"]}
    await fs._hset(HISTORY, str(int(rec["at"] * 1000)), rec)
    try:
        await _sb.audit("system", "ai_model_changed", detail={"model": model_id, "from": rec["from"], "inputs": rec["input_modalities"]})
    except Exception:
        pass
    return rec


def note_model_used(model_id: str) -> None:
    """Called on every model call; a change of model is recorded in the background."""
    global _last_noted
    if model_id == _last_noted:
        return
    _last_noted = model_id
    try:
        asyncio.get_running_loop().create_task(record_change_if_needed(model_id))
    except RuntimeError:
        pass


# ---------------------------------------------------------------- live tests
_DIGITS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"], "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"], "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"], "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"], "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"], "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def test_png(text: str = "4821", scale: int = 10) -> bytes:
    """A small black-on-white picture of some digits (so we can check the model really reads images)."""
    pad = 3 * scale
    w = pad * 2 + len(text) * 6 * scale - scale
    h = pad * 2 + 7 * scale
    rows = []
    for y in range(h):
        row = bytearray([255] * w)
        gy = (y - pad) // scale
        if 0 <= gy < 7 and pad <= y < pad + 7 * scale:
            for i, ch in enumerate(text):
                for gx in range(5):
                    if _DIGITS[ch][gy][gx] == "1":
                        x0 = pad + (i * 6 + gx) * scale
                        for x in range(x0, x0 + scale):
                            row[x] = 0
        rows.append(b"\x00" + bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def test_pdf(text: str = "PETROBIND TEST 4821") -> bytes:
    """A one-page PDF containing a line of text."""
    stream = f"BT /F1 24 Tf 30 100 Td ({text}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 420 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream", b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out)); out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


async def probe(kind: str) -> Dict[str, Any]:
    """Ask the current model something small to prove it works: text, reading a picture, reading a PDF."""
    if kind not in ("text", "image", "document"):
        raise ValueError("kind must be text, image or document")
    model = current_model_id()
    if kind == "text":
        content: Any = "Reply with exactly the word OK."; expected = "OK"
    elif kind == "image":
        b64 = base64.b64encode(test_png("4821")).decode()
        content = [{"type": "text", "text": "What number is written in this image? Reply with the digits only."},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]; expected = "4821"
    else:
        b64 = base64.b64encode(test_pdf("PETROBIND TEST 4821")).decode()
        content = [{"type": "text", "text": "What 4-digit code is written in this document? Reply with the digits only."},
                   {"type": "file", "file": {"filename": "probe.pdf", "file_data": f"data:application/pdf;base64,{b64}"}}]; expected = "4821"
    client = _client()
    if client is None:
        return {"ok": False, "kind": kind, "model": model, "ms": 0, "answer": "", "expected": expected, "error": "No OpenRouter key is set on the server."}
    t0 = time.time()
    try:
        resp = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}], max_tokens=400, timeout=60.0,
                                                    extra_body={"reasoning": {"effort": "low"}})
        answer = (resp.choices[0].message.content or "").strip()
        ok = expected.lower() in answer.lower()
        if kind == "text":
            ai_health.record_success()
        return {"ok": ok, "kind": kind, "model": model, "ms": int((time.time() - t0) * 1000), "answer": answer[:200], "expected": expected,
                "error": "" if ok else f"The model answered {answer[:80]!r} instead of {expected!r}."}
    except Exception as exc:
        info = ai_health.classify(exc)
        if kind == "text":
            ai_health.record_failure(exc)
        return {"ok": False, "kind": kind, "model": model, "ms": int((time.time() - t0) * 1000), "answer": "", "expected": expected,
                "error": f"{info['kind']}: {info['message']}"}
