"""When the AI model cannot answer (e.g. OpenRouter credits used up), people must be told BEFORE any fallback reaches a
buyer, and a price question must still reach the sales director / a human, never a generic 'we'll be in touch'."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import ai_health
import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient
from rag.retrieve import RetrievedChunk

CHUNK = RetrievedChunk(url="https://www.petrobindglobal.com/products/bitumen-60-70", title="Bitumen 60/70",
                       chunk_text="Bitumen 60/70 is a paving grade used for roads.", similarity=0.9, chunk_index=0)
PRICING_LINE = "Hold on, let me check with my sales director about the latest price to confirm."
PH = "60177700042"


class CreditsError(Exception):
    status_code = 402
    def __str__(self): return "Error code: 402 - Insufficient credits. Add more using https://openrouter.ai/settings/credits"


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(ai_health.classify(CreditsError())["kind"], "credits")
        e = Exception("Insufficient credits"); self.assertEqual(ai_health.classify(e)["kind"], "credits")
        e = Exception("bad key"); e.status_code = 401; self.assertEqual(ai_health.classify(e)["kind"], "auth")
        e = Exception("slow down"); e.status_code = 429; self.assertEqual(ai_health.classify(e)["kind"], "rate_limit")
        self.assertEqual(ai_health.classify(RuntimeError("boom"))["kind"], "error")

    def test_state_goes_down_and_recovers(self):
        ai_health.reset()
        self.assertTrue(ai_health.current()["ok"])
        ai_health.record_failure(CreditsError()); ai_health.record_failure(CreditsError())
        c = ai_health.current()
        self.assertEqual((c["ok"], c["kind"], c["failures"]), (False, "credits", 2))
        self.assertIn("openrouter.ai/settings/credits", c["help"]); self.assertTrue(c["since"])
        ai_health.record_success()
        self.assertTrue(ai_health.current()["ok"])

    def test_the_team_email_is_limited_to_one_an_hour_per_problem(self):
        ai_health.reset()
        self.assertTrue(ai_health.should_email("credits")); self.assertFalse(ai_health.should_email("credits"))
        self.assertTrue(ai_health.should_email("auth"))


class Pipeline(unittest.TestCase):
    def setUp(self):
        ai_health.reset()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_unavail_skills.json"; learning.invalidate_cache()
        self.notes, self.audits, self.emails = [], [], []

        async def note(**kw): self.notes.append(kw)
        async def audit(*a, **kw): self.audits.append((a, kw))
        async def email(**kw): self.emails.append(kw); return True
        async def refs(_q): return [CHUNK]

        self._p = [mock.patch.object(L._sbc, "insert_outbound_message", note), mock.patch.object(L._sbc, "audit", audit),
                   mock.patch.object(L.notify, "send_handoff_email", email), mock.patch.object(L, "retrieve", refs),
                   mock.patch.object(L, "index_is_ready", lambda: True), mock.patch.object(L.booking, "is_configured", lambda: True),
                   mock.patch.object(L, "_booking_link_for", lambda *a, **k: "https://cal.com/x")]
        for p in self._p: p.start()

    def tearDown(self):
        for p in self._p: p.stop()
        ai_health.reset()

    def client(self, mode):
        plan = json.dumps({"intent": "price", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})

        async def create(**kw):
            if mode == "credits": raise CreditsError()
            if mode == "empty":
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="", tool_calls=None), finish_reason="stop")])
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="Bitumen 60/70 is a paving grade.", tool_calls=None), finish_reason="stop")])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    def turn(self, text, mode, draft_only=False):
        cs._SESSIONS.pop(PH, None)
        s = cs.ConversationSession(phone_number=PH); s.message_count = 4; cs._SESSIONS[PH] = s
        with mock.patch.object(L, "_openrouter_client", return_value=self.client(mode)):
            return asyncio.run(L.handle_incoming_message(phone_number=PH, inbound_text=text, draft_only=draft_only)), s

    # ---- 1) the price question
    def test_price_question_gets_the_sales_director_line_when_credits_are_out(self):
        out, s = self.turn("I want to buy 500 tonnes of 60/70, what is the price?", "credits")
        self.assertIn(PRICING_LINE, out); self.assertNotIn("Thanks, we'll be in touch", out)
        self.assertIsNotNone(s.needs_human_since)                       # a human is asked to answer
        self.assertIn("pricing", s.needs_human_reason.lower())
        self.assertTrue(any(PH in str(e.get("phone_number")) for e in self.emails))

    def test_price_question_gets_the_line_even_if_the_model_returns_nothing(self):
        out, s = self.turn("how much is bitumen 60/70 per tonne?", "empty")
        self.assertIn(PRICING_LINE, out); self.assertIsNotNone(s.needs_human_since)

    # ---- 2) people are told before the buyer gets a fallback
    def test_credits_out_alerts_the_inbox_before_the_fallback_is_returned(self):
        out, s = self.turn("tell me more about bitumen 60/70", "credits")
        self.assertIn("read more on our website", out)                   # the buyer still gets a polite fallback...
        note = self.notes[0]                                             # ...but a system note is already in the chat
        self.assertEqual((note["direction"], note["e164"]), ("system", PH))
        self.assertIn("credits", note["text"].lower()); self.assertIn("fallback", note["text"].lower())
        self.assertIn("credits", s.needs_human_reason.lower())
        self.assertEqual([a[1].get("action") or a[0][1] for a in self.audits][0], "ai_unavailable")
        self.assertFalse(ai_health.current()["ok"]); self.assertEqual(ai_health.current()["kind"], "credits")

    def test_the_team_is_emailed_once_an_hour_not_for_every_buyer(self):
        self.turn("tell me more about bitumen 60/70", "credits"); self.turn("what is emulsion used for?", "credits")
        alerts = [e for e in self.emails if "AI UNAVAILABLE" in (e.get("reason") or "")]
        self.assertEqual(len(alerts), 1); self.assertIn("openrouter.ai/settings/credits", alerts[0]["partial_inquiry_summary"])

    def test_a_draft_for_the_inbox_pill_has_no_side_effects_but_still_flags_the_outage(self):
        out, s = self.turn("tell me more about bitumen 60/70", "credits", draft_only=True)
        self.assertEqual((self.notes, self.emails), ([], [])); self.assertIsNone(s.needs_human_since)
        self.assertFalse(ai_health.current()["ok"])

    def test_the_next_successful_answer_clears_the_alert(self):
        self.turn("tell me more about bitumen 60/70", "credits"); self.assertFalse(ai_health.current()["ok"])
        self.turn("tell me more about bitumen 60/70", "ok")
        self.assertTrue(ai_health.current()["ok"])


class Api(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        ai_health.reset()
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False; main._LOGIN_FAILS.clear()
        self.c = TestClient(main.app); self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        ai_health.reset()
        if os.path.exists(self._file): os.remove(self._file)

    def test_status_endpoint_needs_login_and_reports_the_outage(self):
        self.assertEqual(TestClient(self.main.app).get("/api/inbox/ai-status").status_code, 401)
        self.assertTrue(self.c.get("/api/inbox/ai-status").json()["ok"])
        ai_health.record_failure(CreditsError())
        j = self.c.get("/api/inbox/ai-status").json()
        self.assertEqual((j["ok"], j["kind"]), (False, "credits")); self.assertIn("credits", j["title"].lower())

    def test_health_reports_whether_the_model_is_working(self):
        self.assertTrue(self.c.get("/health").json()["checks"]["llm_working"])
        ai_health.record_failure(CreditsError())
        r = self.c.get("/health"); self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["checks"]["llm_working"])

    def test_the_ai_suggest_button_shows_the_outage_instead_of_a_canned_draft(self):
        async def fake(**kw):
            ai_health.record_failure(CreditsError()); return "Let me check on that and get back to you shortly."
        with mock.patch.object(self.main.llm_assistant, "handle_incoming_message", fake), \
                mock.patch.object(self.main._sb, "thread_messages", mock.AsyncMock(return_value=[{"direction": "buyer", "text": "hi", "wamid": "w1", "created_at": "2026-10-06T01:00:00Z", "id": 1}])), \
                mock.patch.object(self.main._sb, "ENABLED", True):
            r = self.c.get("/api/inbox/chats/60123456789/suggestion")
        self.assertEqual(r.status_code, 503); self.assertEqual(r.json()["code"], "ai_unavailable")
        self.assertIn("credits", r.json()["detail"].lower())

    def test_every_inbox_page_has_the_status_bar(self):
        html = self.c.get("/inbox/chats").text
        self.assertIn('id="ai-status-bar"', html); self.assertIn("/api/inbox/ai-status", html)


if __name__ == "__main__":
    unittest.main()
