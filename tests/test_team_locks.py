"""Slice 4 (RED first): a chat is held by a PERSON (not a browser tab), shows their name, the admin can release it,
and messages remember who sent them."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

E = "60123456789"


class FakeClaims:
    """Same rules as the inbox_claims table: one holder per chat; the holder refreshes, others conflict."""
    def __init__(self):
        self.rows = {}

    async def claim_acquire(self, *, e164, held_by, session_id, ttl_seconds=120):
        cur = self.rows.get(e164)
        if cur and cur["session_id"] != session_id:
            return False, {"held_by": cur["held_by"], "session_id": cur["session_id"], "expires_in_secs": 100}
        self.rows[e164] = {"held_by": held_by, "session_id": session_id}
        return True, None

    async def claim_is_human_held(self, e164):
        cur = self.rows.get(e164)
        return dict(cur, expires_in_secs=100) if cur else None

    async def claim_release(self, *, e164, held_by, session_id):
        cur = self.rows.get(e164)
        if cur and cur["session_id"] == session_id:
            del self.rows[e164]
        return True

    async def claim_force_release(self, e164):
        return self.rows.pop(e164, None) is not None


class Locks(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(us.create_user(username="raj", name="Raj Kumar", password="raj-password-1"))
        asyncio.run(main._refresh_users_cache())
        self.claims = FakeClaims()
        self.sent = []

        async def fake_send(**kw):
            self.sent.append(kw); return {"messages": [{"id": "wamid.S%d" % len(self.sent)}]}

        self._patches = [
            mock.patch.object(main._sb, "ENABLED", True),
            mock.patch.object(main, "_window_state", mock.AsyncMock(return_value={"last_buyer_at": 1.0, "closes_at": 9e12, "inside": True})),  # these tests are about locks/sending, so the 24h window is open
            mock.patch.object(main._sb, "claim_acquire", self.claims.claim_acquire),
            mock.patch.object(main._sb, "claim_is_human_held", self.claims.claim_is_human_held),
            mock.patch.object(main._sb, "claim_release", self.claims.claim_release),
            mock.patch.object(main._sb, "claim_force_release", self.claims.claim_force_release, create=True),
            mock.patch.object(main._sb, "audit", mock.AsyncMock()),
            mock.patch.object(main, "send_whatsapp_text", fake_send),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def as_(self, username=None):
        c = TestClient(self.main.app)
        data = {"username": username or "", "password": ({"mei": "mei-password-1", "raj": "raj-password-1"}.get(username) or "x" * 24)}
        c.post("/inbox/login", data=data, follow_redirects=False)
        return c

    def take(self, c):
        return c.post(f"/api/inbox/chats/{E}/claim", json={"ttl_seconds": 600})

    def status(self, c):
        return c.get(f"/api/inbox/chats/{E}/claim").json()

    def test_lock_is_held_in_the_members_name_and_others_see_it(self):
        mei, admin = self.as_("mei"), self.as_()
        self.assertEqual(self.take(mei).status_code, 200)
        self.assertEqual(self.status(mei), {"held": True, "mine": True, "held_by": "Mei Ling", "expires_in_secs": 100})
        seen = self.status(admin)
        self.assertEqual((seen["held"], seen["mine"], seen["held_by"]), (True, False, "Mei Ling"))
        r = self.take(admin)
        self.assertEqual((r.status_code, r.json()["held_by"]), (409, "Mei Ling"))

    def test_two_members_cannot_hold_the_same_chat(self):
        mei, raj = self.as_("mei"), self.as_("raj")
        self.take(mei)
        r = self.take(raj)
        self.assertEqual((r.status_code, r.json()["held_by"]), (409, "Mei Ling"))

    def test_the_same_person_in_a_second_browser_is_not_locked_out(self):
        first, second = self.as_("mei"), self.as_("mei")
        self.take(first)
        self.assertEqual(self.take(second).status_code, 200)
        self.assertTrue(self.status(second)["mine"])

    def test_the_admin_in_a_second_browser_is_not_locked_out_either(self):
        a1, a2 = self.as_(), self.as_()
        self.take(a1)
        self.assertEqual(self.take(a2).status_code, 200)

    def test_member_cannot_send_into_a_chat_the_admin_holds(self):
        admin, mei = self.as_(), self.as_("mei")
        self.take(admin)
        r = mei.post(f"/api/inbox/chats/{E}/messages", json={"text": "hello"})
        self.assertEqual((r.status_code, r.json()["held_by"]), (409, "Petrobind Admin"))
        self.assertEqual(self.sent, [])

    def test_a_sent_message_remembers_who_sent_it(self):
        mei = self.as_("mei"); self.take(mei)
        r = mei.post(f"/api/inbox/chats/{E}/messages", json={"text": "hello"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.sent[0]["sender_direction"], self.sent[0]["sent_by"]), ("human", "Mei Ling"))

    def test_sending_without_taking_the_chat_first_holds_it_in_your_name(self):
        mei = self.as_("mei")
        mei.post(f"/api/inbox/chats/{E}/messages", json={"text": "hello"})
        self.assertEqual(self.claims.rows[E]["held_by"], "Mei Ling")

    def test_a_member_releases_only_their_own_lock(self):
        mei, raj = self.as_("mei"), self.as_("raj")
        self.take(mei)
        raj.delete(f"/api/inbox/chats/{E}/claim")
        self.assertIn(E, self.claims.rows)                      # Raj cannot release Mei's lock
        mei.delete(f"/api/inbox/chats/{E}/claim")
        self.assertNotIn(E, self.claims.rows)

    def test_only_the_admin_can_force_release_someone_elses_lock(self):
        mei, raj, admin = self.as_("mei"), self.as_("raj"), self.as_()
        self.take(mei)
        self.assertEqual(raj.delete(f"/api/inbox/chats/{E}/claim?force=1").status_code, 403)
        self.assertIn(E, self.claims.rows)
        self.assertEqual(admin.delete(f"/api/inbox/chats/{E}/claim?force=1").status_code, 200)
        self.assertNotIn(E, self.claims.rows)


class MessagesRememberTheSender(unittest.TestCase):
    def _fake_client(self, captured, rows=None):
        class Resp:
            status_code = 200
            def __init__(s, data): s._d = data
            def json(s): return s._d
        class Client:
            def __init__(s, *a, **k): pass
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            async def post(s, url, headers=None, json=None, **k):
                captured.append((url, json)); return Resp({})
            async def get(s, url, headers=None, params=None, **k):
                return Resp(rows or [])
        return Client

    def test_outbound_row_stores_the_sender_name(self):
        captured = []
        with mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_REST_BASE", "http://x/rest/v1"), mock.patch.object(sb.httpx, "AsyncClient", self._fake_client(captured)):
            asyncio.run(sb.insert_outbound_message(e164=E, direction="human", text="hi", sent_by="Mei Ling"))
        (url, body), = captured
        self.assertEqual(body["payload_jsonb"], {"sent_by": "Mei Ling"})

    def test_thread_shows_who_sent_each_message(self):
        rows = [{"id": 1, "direction": "human", "text": "hi", "payload_jsonb": {"sent_by": "Mei Ling"}},
                {"id": 2, "direction": "ai", "text": "hello", "payload_jsonb": {}}]
        with mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_REST_BASE", "http://x/rest/v1"), mock.patch.object(sb.httpx, "AsyncClient", self._fake_client([], rows)):
            out = asyncio.run(sb.thread_messages(E))
        by_id = {m["id"]: m for m in out}
        self.assertEqual((by_id[1].get("held_by"), by_id[2].get("held_by")), ("Mei Ling", None))


if __name__ == "__main__":
    unittest.main()
