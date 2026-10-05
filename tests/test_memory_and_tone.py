"""RED first: (1) an admin can make the AI forget a chat; (2) the AI must not keep reminding buyers about an earlier
order, it chats casually until the buyer brings the order up."""
import asyncio
import json
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""
os.environ.setdefault("OPENROUTER_API_KEY", "test")

import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
import reply_guard as g
import supabase_client as sb

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "petrobind_frontend_app", "templates")


class OrderReminderGuard(unittest.TestCase):
    def test_detects_when_the_buyer_raises_an_order(self):
        for t in ("any update on my order?", "can you send the quote again", "what about the shipment", "PO number is 123", "invoice please"):
            self.assertTrue(g.buyer_mentions_order(t), t)
        for t in ("hi, how are you?", "what's the weather like in KL", "thanks!", "do you also sell base oil?"):
            self.assertFalse(g.buyer_mentions_order(t), t)

    def test_unprompted_reminders_are_removed(self):
        reply = "Doing well, thanks for asking! Just following up on your earlier order of Bitumen 60/70. Anything fun planned this week?"
        out = g.strip_order_reminders(reply, "how are you doing today?")
        self.assertNotIn("following up", out.lower())
        self.assertIn("Doing well", out)
        self.assertIn("Anything fun planned", out)

    def test_reminders_are_kept_when_the_buyer_asked_about_the_order(self):
        reply = "Your earlier order of 20 tonnes is being prepared. I'll confirm the loading date soon."
        self.assertEqual(g.strip_order_reminders(reply, "any update on my order?"), reply)

    def test_never_returns_an_empty_reply(self):
        only = "Just following up on your previous inquiry."
        self.assertEqual(g.strip_order_reminders(only, "hello"), only)

    def test_normal_current_flow_questions_are_not_touched(self):
        reply = "Got it. How many tonnes are you looking at, and which port should it go to?"
        self.assertEqual(g.strip_order_reminders(reply, "ok"), reply)


class TonePromptAndSkill(unittest.TestCase):
    def test_memory_note_says_not_to_bring_up_earlier_orders(self):
        s = cs.ConversationSession(phone_number="1"); s.inquiry.product = "Bitumen 60/70"; s.inquiry.quantity = "20 tonnes"
        note = L._format_buyer_memory(s)
        self.assertIn("Do NOT bring up", note)
        self.assertIn("unless the buyer", note)

    def test_builtin_skill_keeps_chat_casual_and_unpushy(self):
        sk = {b["id"]: b for b in learning.BUILTIN_SKILLS}
        self.assertIn("builtin-casual-not-pushy", sk)
        self.assertTrue(sk["builtin-casual-not-pushy"]["always"])


class ForgetMemory(unittest.TestCase):
    def setUp(self):
        cs._REDIS_URL = cs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tmp.close(); os.remove(tmp.name)
        sb._AUDIT_FILE = self._audit = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = main.INBOX_ADMIN_TOKEN or "t" * 20
        main._sb.ENABLED = False
        from fastapi.testclient import TestClient
        self.c = TestClient(main.app)
        self.H = {"Authorization": "Bearer " + main.INBOX_ADMIN_TOKEN}

    def tearDown(self):
        if os.path.exists(self._audit):
            os.remove(self._audit)

    def test_forgetting_clears_ai_memory_and_is_logged(self):
        s = cs.ConversationSession(phone_number="60188800077")
        s.append("user", "I want 20 tonnes of 60/70"); s.append("assistant", "Noted, 20 tonnes.")
        s.inquiry.product = "Bitumen 60/70"; s.needs_human_since = 1.0
        cs._SESSIONS["60188800077"] = s
        r = self.c.post("/api/inbox/chats/%2B60188800077/forget-memory", headers=self.H)
        self.assertEqual(r.status_code, 200); self.assertTrue(r.json()["ok"])
        self.assertNotIn("60188800077", cs._SESSIONS)
        fresh = asyncio.run(cs.get_session("60188800077"))
        self.assertEqual((fresh.history, fresh.inquiry.product, fresh.needs_human_since), ([], None, None))
        ev = asyncio.run(sb.list_audit(limit=5, action="memory_reset"))
        self.assertEqual(len(ev), 1)

    def test_requires_auth(self):
        self.assertEqual(self.c.post("/api/inbox/chats/1/forget-memory").status_code, 401)

    def test_menu_offers_forget_ai_memory_with_a_confirmation_dialog(self):
        html = open(os.path.join(TEMPLATES, "chat_list.html"), encoding="utf-8").read()
        self.assertIn("Forget AI memory", html)
        self.assertIn('id="forget-dialog"', html)


class SupabaseSummaryReset(unittest.TestCase):
    """The live Supabase call can't run in tests, so check the exact request it would send."""

    def test_blanks_the_summary_but_never_touches_messages(self):
        calls = []

        class FakeResp:
            status_code = 204

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def patch(self, url, headers=None, params=None, json=None):
                calls.append((url, params, json)); return FakeResp()

        with mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_REST_BASE", "http://x/rest/v1"), mock.patch.object(sb.httpx, "AsyncClient", FakeClient):
            ok = asyncio.run(sb.reset_session_mirror(["60123", "+60123"]))
        self.assertTrue(ok)
        (url, params, body), = calls
        self.assertTrue(url.endswith("/sessions"), "only the session summary is reset, never /messages")
        self.assertEqual(params["e164"], 'in.("60123","+60123")')
        self.assertEqual((body["inquiry_jsonb"], body["history_jsonb"], body["lead_notified"], body["handoff_notified"]), ({}, [], False, False))

    def test_is_a_no_op_without_supabase(self):
        with mock.patch.object(sb, "ENABLED", False):
            self.assertFalse(asyncio.run(sb.reset_session_mirror(["1"])))


class PipelineDropsUnpromptedReminders(unittest.TestCase):
    def _turn(self, buyer_text, llm_reply):
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_tone_skills.json"; learning.invalidate_cache()
        plan = json.dumps({"intent": "small talk", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})
        seq = [plan, llm_reply]; n = {"i": 0}

        async def create(**kw):
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def no_refs(_q):
            return []

        cs._SESSIONS.pop("60177700001", None)
        s = cs.ConversationSession(phone_number="60177700001"); s.inquiry.product = "Bitumen 60/70"; s.message_count = 3
        cs._SESSIONS["60177700001"] = s
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", no_refs), mock.patch.object(L, "index_is_ready", lambda: False):
            return asyncio.run(L.handle_incoming_message(phone_number="60177700001", inbound_text=buyer_text))

    def test_casual_chat_does_not_get_an_order_reminder(self):
        out = self._turn("how's your week going?", "Pretty good, thanks! Just following up on your earlier order of Bitumen 60/70. How about you?")
        self.assertNotIn("following up", out.lower())
        self.assertIn("Pretty good", out)

    def test_order_questions_still_get_order_answers(self):
        out = self._turn("any update on my order?", "Your earlier order of Bitumen 60/70 is being prepared. I'll confirm the date soon.")
        self.assertIn("earlier order", out)


if __name__ == "__main__":
    unittest.main()
