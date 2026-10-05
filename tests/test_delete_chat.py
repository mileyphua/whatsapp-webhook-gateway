"""Deleting a chat (admin OR team member) must remove it from the left-hand list for good, and stop anything queued for it."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store
import users_store as us
from fastapi.testclient import TestClient

NUMBER = "60177000111"


class DeleteChat(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(main._refresh_users_cache())
        self.agent = TestClient(main.app)
        self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        self.admin = TestClient(main.app)
        self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.audits = []
        self._a = mock.patch.object(main._sb, "audit", mock.AsyncMock(side_effect=lambda **kw: self.audits.append(kw)))
        self._a.start()
        s = asyncio.run(conversation_store.get_session(NUMBER))
        s.append("user", "Hello, do you sell bitumen?")
        s.append("assistant", "Yes we do. How many tonnes?")

    def tearDown(self):
        self._a.stop()
        asyncio.run(conversation_store.reset_session(NUMBER))
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def listed(self, client):
        return [c["e164"] for c in client.get("/api/inbox/chats").json()["chats"]] if "chats" in client.get("/api/inbox/chats").json() else \
               [c["e164"] for c in client.get("/api/inbox/chats").json()]

    def test_chat_is_listed_before_delete(self):
        self.assertIn(NUMBER, self.listed(self.admin))

    def test_admin_delete_removes_it_from_the_list_api_and_the_page(self):
        r = self.admin.delete(f"/api/inbox/chats/{NUMBER}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(NUMBER, self.listed(self.admin))
        self.assertNotIn(NUMBER, self.admin.get("/inbox/chats").text)

    def test_team_member_can_delete_and_is_recorded_as_the_actor(self):
        r = self.agent.delete(f"/api/inbox/chats/{NUMBER}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(NUMBER, self.listed(self.agent))
        self.assertNotIn(NUMBER, self.admin.get("/inbox/chats").text)
        self.assertEqual([a["actor"] for a in self.audits if a["action"] == "chat_delete"], ["Mei Ling"])

    def test_delete_button_is_shown_to_team_members(self):
        html = self.agent.get("/inbox/chats").text
        i = html.index('id="act-delete"')
        self.assertNotIn("hidden", html[i:html.index(">", i)])

    def test_logged_out_visitor_cannot_delete(self):
        r = TestClient(self.main.app).delete(f"/api/inbox/chats/{NUMBER}")
        self.assertEqual(r.status_code, 401)
        self.assertIn(NUMBER, self.listed(self.admin))

    def test_delete_clears_the_lock_too(self):
        self.main._sb._LOCAL_CLAIMS[NUMBER] = {"held_by": "Mei Ling", "session_id": "u:mei", "expires_at": 9e12}
        self.admin.delete(f"/api/inbox/chats/{NUMBER}")
        self.assertNotIn(NUMBER, self.main._sb._LOCAL_CLAIMS)


class SupabaseDelete(unittest.TestCase):
    def test_delete_cancels_pending_scheduled_sends_for_that_number(self):
        import supabase_client as sb
        calls = []

        class FakeResp:
            status_code = 204
            headers = {"content-range": "*/1"}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def delete(self, url, **kw): calls.append(("DELETE", url.rsplit("/", 1)[-1], kw.get("params"))); return FakeResp()
            async def patch(self, url, **kw): calls.append(("PATCH", url.rsplit("/", 1)[-1], kw.get("params"), kw.get("json"))); return FakeResp()

        with mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb.httpx, "AsyncClient", FakeClient):
            asyncio.run(sb.delete_chat(["60177000111", "+60177000111"]))
        patched = [c for c in calls if c[0] == "PATCH" and c[1] == "outbound_schedules"]
        self.assertEqual(len(patched), 1, calls)
        self.assertEqual(patched[0][3], {"status": "cancelled"})
        self.assertIn("pending", str(patched[0][2]))


if __name__ == "__main__":
    unittest.main()
