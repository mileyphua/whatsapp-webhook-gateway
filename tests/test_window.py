"""WhatsApp's 24-hour rule: free-text messages are allowed only for 24h after the BUYER's last message.
The countdown must run from that message, our own messages must never reopen the window, and the server must
refuse a free-text send once the window is closed (instead of letting Meta reject it)."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store
import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

N = "60144000555"
PDF = b"%PDF-1.7\n" + b"x" * 300


class FakeMeta:
    posts = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None, data=None, files=None, **k):
        FakeMeta.posts.append((url, json))
        class R:
            status_code = 200; content = b"{}"; text = "{}"
            def json(s): return {"id": "MEDIA1", "messages": [{"id": "wamid.SENT1"}]}
        return R()


class Base(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        FakeMeta.posts = []
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        self._enabled = sb.ENABLED; sb.ENABLED = False
        self._store = sb._LOCAL_OUTBOUND_FILE; sb._LOCAL_OUTBOUND_FILE = tempfile.mktemp(suffix=".json")
        sb._LOCAL_OUTBOUND.clear(); sb._LOCAL_CLAIMS.clear()
        main._LOGIN_FAILS.clear()
        self._p = [mock.patch.object(main, "ACCESS_TOKEN", "tok"), mock.patch.object(main, "PHONE_NUMBER_ID", "PN1"),
                   mock.patch.object(main.httpx, "AsyncClient", FakeMeta)]
        for p in self._p: p.start()
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        for p in self._p: p.stop()
        sb.ENABLED = self._enabled
        if os.path.exists(sb._LOCAL_OUTBOUND_FILE): os.remove(sb._LOCAL_OUTBOUND_FILE)
        sb._LOCAL_OUTBOUND_FILE = self._store
        sb._LOCAL_OUTBOUND.clear(); sb._LOCAL_CLAIMS.clear()
        asyncio.run(conversation_store.reset_session(N))
        if os.path.exists(self._file): os.remove(self._file)

    def buyer_wrote(self, hours_ago, wamid="wamid.B1"):
        ts = int(time.time() - hours_ago * 3600)
        asyncio.run(self.main._persist_inbound_safe({"id": wamid, "from": N, "type": "text", "text": {"body": "hi"}, "timestamp": str(ts)}))
        return ts

    def we_wrote(self, text="our message"):
        asyncio.run(self.main._persist_outbound_safe(e164=N, direction="human", text=text, sent_id_from_graph="wamid.O1"))

    def window(self, path=N):
        return self.c.get(f"/api/inbox/window-check/{path}").json()

    def send_text(self):
        return self.c.post(f"/api/inbox/chats/{N}/messages", json={"text": "hello buyer"})

    def sent_to_meta(self):
        return [p for p in FakeMeta.posts if p[1] and p[1].get("type") in ("text", "document", "image")]


class CountdownRunsFromTheBuyersLastMessage(Base):
    def test_window_closes_exactly_24h_after_the_buyers_last_message(self):
        ts = self.buyer_wrote(2)
        w = self.window()
        self.assertTrue(w["inside_24h_window"]); self.assertEqual(w["window_closes_at_unix_ts"], ts + 86400)

    def test_a_new_buyer_message_restarts_the_countdown(self):
        self.buyer_wrote(20, "wamid.B1")
        ts2 = self.buyer_wrote(1, "wamid.B2")
        self.assertEqual(self.window()["window_closes_at_unix_ts"], ts2 + 86400)

    def test_after_24h_the_window_is_closed(self):
        self.buyer_wrote(25)
        w = self.window(); self.assertFalse(w["inside_24h_window"])

    def test_our_own_messages_never_open_or_extend_the_window(self):
        self.we_wrote("template sent to a new contact")
        w = self.window()
        self.assertFalse(w["inside_24h_window"]); self.assertIsNone(w["window_closes_at_unix_ts"])
        ts = self.buyer_wrote(23)
        self.we_wrote("we replied just now")
        self.assertEqual(self.window()["window_closes_at_unix_ts"], ts + 86400)

    def test_the_ai_session_fallback_uses_the_buyers_time_not_the_last_activity(self):
        s = asyncio.run(conversation_store.get_session(N))
        s.append("user", "old question"); s.last_buyer_ts = time.time() - 30 * 3600
        s.append("assistant", "answer")                       # AI activity just now must not reopen the window
        self.assertFalse(self.window()["inside_24h_window"])

    def test_number_format_does_not_matter(self):
        self.buyer_wrote(1)
        self.assertTrue(self.window("%2B60144000555")["inside_24h_window"])


class FreeTextOnlyWhileTheWindowIsOpen(Base):
    def test_text_can_be_sent_while_open(self):
        self.buyer_wrote(3)
        r = self.send_text(); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.sent_to_meta()), 1)

    def test_text_is_refused_before_reaching_meta_when_closed(self):
        self.buyer_wrote(30)
        r = self.send_text()
        self.assertEqual(r.status_code, 422); self.assertEqual(r.json()["code"], "window_closed")
        self.assertIn("template", r.json()["detail"].lower()); self.assertEqual(self.sent_to_meta(), [])

    def test_a_chat_the_buyer_never_wrote_in_is_closed(self):
        self.we_wrote("template")
        self.assertEqual(self.send_text().status_code, 422)

    def test_attachments_follow_the_same_rule(self):
        up = lambda: self.c.post(f"/api/inbox/chats/{N}/attachments", files={"file": ("specs.pdf", PDF, "application/pdf")})
        self.buyer_wrote(30)
        r = up(); self.assertEqual((r.status_code, r.json()["code"]), (422, "window_closed")); self.assertEqual(self.sent_to_meta(), [])
        self.buyer_wrote(1, "wamid.B9")
        self.assertEqual(up().status_code, 200); self.assertEqual(len(self.sent_to_meta()), 1)

    def test_reopens_when_the_buyer_writes_again(self):
        self.buyer_wrote(30)
        self.assertEqual(self.send_text().status_code, 422)
        self.buyer_wrote(0.01, "wamid.B2")
        self.assertEqual(self.send_text().status_code, 200)

    def test_templates_are_still_allowed_when_closed(self):
        self.buyer_wrote(30)
        with mock.patch.object(self.main, "_approved_templates", mock.AsyncMock(return_value=[{
                "name": "t1", "language": "en_US", "supported": True, "header_format": "", "category": "UTILITY",
                "components": [{"type": "BODY", "text": "Hello", "parameters": []}]}])):
            r = self.c.post("/api/inbox/send-template", json={"template_name": "t1", "language": "en_US", "e164": N, "params": {}})
        self.assertEqual(r.status_code, 200, r.text)


class SupabaseLookup(unittest.TestCase):
    def test_looks_up_both_number_formats_and_falls_back_to_the_messages_table(self):
        calls = []

        class Resp:
            status_code = 200
            def __init__(s, rows): s._r = rows
            def json(s): return s._r

        class Client:
            def __init__(s, *a, **k): pass
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            async def get(s, url, headers=None, params=None, **k):
                calls.append((url.rsplit("/", 1)[-1], params))
                return Resp([]) if url.endswith("/sessions") else Resp([{"created_at": "2026-10-06T01:00:00+00:00"}])

        with mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_client", lambda: Client()):
            ts = asyncio.run(sb.get_last_buyer_message_at("+60144000555"))
        self.assertIsNotNone(ts)
        self.assertIn("in.(", calls[0][1]["e164"]); self.assertIn("60144000555", calls[0][1]["e164"]); self.assertNotIn("%2B", str(calls[0][1]))
        self.assertEqual(calls[1][0], "messages"); self.assertEqual(calls[1][1]["direction"], "eq.buyer")


class NotifyAndFollowups(Base):
    def test_main_can_send_handoff_emails(self):
        self.assertTrue(hasattr(self.main, "notify"))

    def test_buyer_media_emails_the_team_once_an_hour_not_once_per_photo(self):
        main = self.main
        sent = []

        async def fake_email(**kw): sent.append(kw["phone_number"]); return True

        async def not_held(_n, _s=None): return None

        def img(i): return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"}, "messages": [{"id": f"wamid.IMG{i}", "from": N, "type": "image", "timestamp": str(int(time.time())),
                                                                   "image": {"id": f"M{i}12345", "mime_type": "image/jpeg"}}]}}]}]}

        main._MEDIA_EMAIL_LAST.clear()
        with mock.patch.object(main, "WHATSAPP_APP_SECRET", ""), mock.patch.object(main.notify, "send_handoff_email", fake_email), \
                mock.patch.object(main, "_claim_is_held_by_other", not_held), TestClient(main.app) as c:
            for i in range(3):
                c.post("/webhook", json=img(i))
            self.assertEqual(sent, [N])
            main._MEDIA_EMAIL_LAST[N] -= 2 * 3600
            c.post("/webhook", json=img(9))
        self.assertEqual(sent, [N, N])

    def test_followups_read_the_buyers_last_message_time_from_the_session(self):
        s = asyncio.run(conversation_store.get_session(N)); s.last_buyer_ts = 1234.5
        self.assertEqual(self.main._session_last_buyer_ts(N), 1234.5)
        self.assertIsNone(self.main._session_last_buyer_ts("60100000000"))


class HealthSeesTheKnowledgeIndex(Base):
    def test_health_really_asks_rag_instead_of_failing_silently(self):
        for ready in (True, False):
            with mock.patch.object(self.main.rag, "index_is_ready", lambda r=ready: r):
                r = self.c.get("/health")
            self.assertEqual(r.status_code, 200)               # health is always 200 so Render never rolls back
            self.assertEqual(r.json()["checks"]["rag_index_ready"], ready)


class CountdownScript(unittest.TestCase):
    def setUp(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "petrobind_frontend_app", "static", "app.js")).read()
        self.js = js
        a = js.index("function tickHeaderCountdown()"); self.tick = js[a:a + 2200]

    def test_the_composer_closes_the_moment_the_countdown_reaches_zero(self):
        self.assertIn("renderWindowBanner(false", self.tick)

    def test_a_refused_send_refreshes_the_window_state(self):
        a = self.js.index("window_closed"); self.assertIn("pollWindowStatus", self.js[a - 400:a + 400])


if __name__ == "__main__":
    unittest.main()
