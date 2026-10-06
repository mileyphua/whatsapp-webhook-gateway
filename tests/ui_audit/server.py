"""Throwaway inbox for the button audit: local mode (no Supabase/Redis), WhatsApp replaced by a fake, two seeded chats.
Run:  python3 tests/ui_audit/server.py PORT      Chats: 60120000001 (buyer wrote 2h ago, window OPEN), 60120000002 (30h ago, CLOSED)."""
import asyncio
import os
import sys
import tempfile
import time

for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "RENDER_INBOX_URL"):
    os.environ[k] = ""          # never touch the live Redis / Supabase / Render that the developer's .env points at
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
tmp = tempfile.mkdtemp(prefix="ui_audit_")

import supabase_client as sb  # noqa: E402  (first: set its private files before anything else loads the real ones)
sb._AUDIT_FILE = os.path.join(tmp, "audit.jsonl")
sb._LOCAL_OUTBOUND_FILE = os.path.join(tmp, "outbound.json")
sb._LOCAL_OUTBOUND.clear()
import learning, contact_names, feedback_store, users_store, conversation_store  # noqa: E402
users_store._REDIS_URL = users_store._REDIS_TOKEN = ""
users_store._FILE = os.path.join(tmp, "users.json")
contact_names._FILE = os.path.join(tmp, "names.json")
feedback_store._FILE = os.path.join(tmp, "learning.json")
sb.ENABLED = False

import site_links  # noqa: E402


async def _fake_site_check(url):      # the website is NOT reachable from tests: pretend only the 60/70 page opens
    return {"ok": url.endswith("/products/bitumen-60-70"), "status": 200 if url.endswith("/products/bitumen-60-70") else 404, "checked_at": 0}

site_links.check = _fake_site_check
import model_info  # noqa: E402

_GPT5_MINI = {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini", "description": "Compact GPT-5 (audit copy).", "context_length": 400000,
              "architecture": {"input_modalities": ["text", "image", "file"], "output_modalities": ["text"]},
              "pricing": {"prompt": "0.00000025", "completion": "0.000002"}, "top_provider": {"max_completion_tokens": 128000, "is_moderated": True},
              "supported_parameters": ["max_tokens", "reasoning", "reasoning_effort", "response_format", "seed", "tool_choice", "tools"], "knowledge_cutoff": "2024-05-31"}


async def _fake_models(force=False):
    return [_GPT5_MINI]


class _FakeModelClient:
    class chat:
        class completions:
            @staticmethod
            async def create(**kw):
                import types as _t
                content = kw["messages"][-1]["content"]
                answer = "4821" if isinstance(content, list) else "OK"
                return _t.SimpleNamespace(choices=[_t.SimpleNamespace(message=_t.SimpleNamespace(content=answer))])


model_info._models = _fake_models
model_info._client = lambda: _FakeModelClient()
import main  # noqa: E402
import uvicorn  # noqa: E402

main.INBOX_ADMIN_TOKEN = "audit-admin-pass-123456"
main.WHATSAPP_APP_SECRET = ""
main.WABA_ID, main.ACCESS_TOKEN, main.PHONE_NUMBER_ID = "WABA1", "fake-token", "PNID1"
main._global_proactive_rate_limiter = lambda k: (True, 0)
main._per_e164_rate_limiter = lambda k: (True, 0)

TEMPLATES = [
    {"name": "enquiry_followup", "category": "UTILITY", "language": "en_US", "status": "APPROVED",
     "components": [{"type": "BODY", "text": "Hello {{1}}, following up on {{2}}. Reply here."}]},
    {"name": "introductory_follow_up", "category": "UTILITY", "language": "en", "status": "APPROVED",
     "components": [{"type": "HEADER", "format": "DOCUMENT"}, {"type": "BODY", "text": "Hi {{1}}, this is {{2}} from Petrobind."}]},
    {"name": "has_video", "category": "MARKETING", "language": "en", "status": "APPROVED",
     "components": [{"type": "HEADER", "format": "VIDEO"}, {"type": "BODY", "text": "Watch"}]},
]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SENT = []


class FakeWhatsApp:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    @staticmethod
    def _r(status, body=None, content=b"", ctype="application/json"):
        class R:
            headers = {"content-type": ctype}; text = str(body)
        R.status_code = status; R.content = content or b"{}"
        R.json = lambda self=None: body
        return R()

    async def get(self, url, headers=None, params=None, **k):
        if "/message_templates" in url:
            return self._r(200, {"data": TEMPLATES})
        if url.startswith("https://graph.facebook.com/") and "/WABA1" not in url:
            return self._r(200, {"url": "https://lookaside.fbsbx.com/x", "mime_type": "image/png", "file_size": len(PNG)})
        return self._r(200, None, PNG, "image/png")

    async def post(self, url, json=None, headers=None, data=None, files=None, **k):
        if url.endswith("/media"):
            SENT.append({"kind": "media-upload", "file": files["file"][0]})
            return self._r(200, {"id": "MEDIA_AUDIT_1"})
        SENT.append({"kind": "message", "body": json})
        return self._r(200, {"messages": [{"id": "wamid.AUDIT%d" % len(SENT)}]})


main.httpx.AsyncClient = FakeWhatsApp


async def fake_handle(**kw):
    return "Audit suggested reply."

main.llm_assistant.handle_incoming_message = fake_handle


async def fake_learn(*a, **k):
    return {"skills": [{"name": "Pricing questions", "always": False, "description": "Buyer asks how much something costs.",
                        "instructions": "Ask for quantity and destination first, because price depends on both."}],
            "knowledge_checks": ["Check VG30 lead time"]}

learning._llm_json = fake_learn


class _Credits(Exception):
    status_code = 402
    def __str__(self): return "Error code: 402 - Insufficient credits"


@main.app.get("/_ai_fail")
async def _ai_fail():
    main.ai_health.record_failure(_Credits()); return {"ok": False}


@main.app.get("/_ai_ok")
async def _ai_ok():
    main.ai_health.record_success(); return {"ok": True}


@main.app.get("/_sent")
async def _sent():
    return SENT

for _ in range(3):     # the test routes must sit in front of the app's catch-all route
    main.app.router.routes.insert(0, main.app.router.routes.pop())


SEEDED = []


async def seed():
    if SEEDED:
        return
    SEEDED.append(1)
    now = time.time()
    A, B = "60120000001", "60120000002"

    async def buyer(n, text, hours, wamid, **extra):
        msg = {"id": wamid, "from": n, "type": "text", "text": {"body": text}, "timestamp": str(int(now - hours * 3600))}
        msg.update(extra)
        await main._persist_inbound_safe(msg)
        await main._note_buyer_message(n, now - hours * 3600)

    await buyer(A, "Hello, what is your bitumen 60/70 price?", 2.0, "wamid.A1")
    await main._persist_outbound_safe(e164=A, direction="ai", text="Hi, this is Jane from Petrobind. How many tonnes do you need?", sent_id_from_graph="wamid.AI1")
    await buyer(A, "", 1.0, "wamid.A2", type="image", image={"id": "IMGAUDIT123", "mime_type": "image/png", "caption": "our site"})
    await buyer(B, "Hi, are you open?", 30.0, "wamid.B1")
    await main._persist_outbound_safe(e164=B, direction="ai", text="Yes, how can I help?", sent_id_from_graph="wamid.AI2")
    P = "60120000003"                                      # the AI could not read a voice note here: paused until a person replies
    await buyer(P, "", 1.0, "wamid.P1", type="audio", audio={"id": "AUDAUDIT1", "mime_type": "audio/ogg"})
    sp = await conversation_store.get_session(P)
    sp.ai_paused, sp.ai_paused_at = True, now - 3000
    sp.ai_paused_reason = "AI paused: the buyer sent a voice note. A person needs to take over and reply."
    sp.needs_human_since, sp.needs_human_reason = now - 3000, sp.ai_paused_reason
    await main._persist_outbound_safe(e164=P, direction="system", text="⚠ The buyer sent a voice note, which the AI can't read. The AI is paused for this chat and will not reply to anything until a person takes over. Please take over this chat and reply to the buyer.")
    s = await conversation_store.get_session(B)
    s.needs_human_since, s.needs_human_reason = now - 600, "buyer asked for a person"
    for n in (1, 2):
        await feedback_store.add_feedback(e164=A, ai_text=f"Audit AI reply {n}: the price is $500.", buyer_text=f"audit price question {n}?",
                                          rating="down", note="Do not quote prices", better_reply="Depends on volume.", actor="Admin")
    try:
        await users_store.create_user(username="mei", name="Mei Ling", password="mei-password-1")
    except ValueError:
        pass                                   # already there (startup can run more than once)
    await main._refresh_users_cache()

main.app.add_event_handler("startup", seed)

if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
