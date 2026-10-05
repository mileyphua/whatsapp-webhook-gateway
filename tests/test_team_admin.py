"""Slice 5 (RED first): the admin adds, edits, disables and removes team members."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient


class TeamAdmin(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        self.audit = mock.AsyncMock()
        self._p = mock.patch.object(main._sb, "audit", self.audit); self._p.start()
        self.admin = TestClient(main.app)
        self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        self._p.stop()
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def add(self, username="mei", name="Mei Ling", password="mei-password-1"):
        return self.admin.post("/api/inbox/team", json={"username": username, "name": name, "password": password})

    def login(self, username, password):
        c = TestClient(self.main.app)
        r = c.post("/inbox/login", data={"username": username, "password": password}, follow_redirects=False)
        return c, r.headers["location"]

    def test_admin_adds_a_member_who_can_log_in_straight_away(self):
        r = self.add()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["member"], {"username": "mei", "name": "Mei Ling", "disabled": False, "created_ts": mock.ANY})
        c, loc = self.login("mei", "mei-password-1")
        self.assertEqual(loc, "/inbox/chats")
        self.assertEqual(c.get("/api/inbox/me").json()["name"], "Mei Ling")

    def test_listing_shows_members_without_any_password_data(self):
        self.add(); body = self.admin.get("/api/inbox/team").json()
        self.assertEqual([m["username"] for m in body["members"]], ["mei"])
        self.assertNotIn("pw_hash", str(body)); self.assertNotIn("mei-password-1", str(body))

    def test_bad_input_is_refused_with_a_clear_message(self):
        self.add()
        for u, p in (("mei", "mei-password-1"), ("admin", "long-enough-1"), ("ab", "long-enough-1"), ("new.user", "short")):
            r = self.add(username=u, password=p)
            self.assertEqual(r.status_code, 422, (u, p)); self.assertTrue(r.json()["detail"])

    def test_disabling_blocks_login_and_ends_open_sessions_at_once(self):
        self.add(); c, _ = self.login("mei", "mei-password-1")
        self.assertEqual(c.get("/api/inbox/me").status_code, 200)
        self.assertEqual(self.admin.patch("/api/inbox/team/mei", json={"disabled": True}).status_code, 200)
        self.assertEqual(c.get("/api/inbox/me").status_code, 401)            # no waiting for a cache refresh
        self.assertIn("error=", self.login("mei", "mei-password-1")[1])
        self.admin.patch("/api/inbox/team/mei", json={"disabled": False})
        self.assertEqual(self.login("mei", "mei-password-1")[1], "/inbox/chats")

    def test_resetting_a_password_replaces_the_old_one(self):
        self.add()
        self.admin.patch("/api/inbox/team/mei", json={"password": "a brand new pass"})
        self.assertIn("error=", self.login("mei", "mei-password-1")[1])
        self.assertEqual(self.login("mei", "a brand new pass")[1], "/inbox/chats")

    def test_renaming_shows_the_new_name(self):
        self.add(); c, _ = self.login("mei", "mei-password-1")
        self.admin.patch("/api/inbox/team/mei", json={"name": "Mei L."})
        self.assertEqual(c.get("/api/inbox/me").json()["name"], "Mei L.")

    def test_deleting_removes_the_member_and_ends_their_session(self):
        self.add(); c, _ = self.login("mei", "mei-password-1")
        self.assertEqual(self.admin.delete("/api/inbox/team/mei").status_code, 200)
        self.assertEqual(c.get("/api/inbox/me").status_code, 401)
        self.assertEqual(self.admin.get("/api/inbox/team").json()["members"], [])

    def test_unknown_member_is_a_404(self):
        self.assertEqual(self.admin.patch("/api/inbox/team/ghost", json={"disabled": True}).status_code, 404)

    def test_members_cannot_manage_the_team_and_strangers_get_401(self):
        self.add(); c, _ = self.login("mei", "mei-password-1")
        for method, path, body in (("GET", "/api/inbox/team", None), ("POST", "/api/inbox/team", {"username": "x1y", "name": "X", "password": "long-enough-1"}),
                                   ("PATCH", "/api/inbox/team/mei", {"disabled": True}), ("DELETE", "/api/inbox/team/mei", None)):
            r = c.request(method, path, json=body) if body is not None else c.request(method, path)
            self.assertEqual(r.status_code, 403, (method, path))
            anon = TestClient(self.main.app)
            r = anon.request(method, path, json=body) if body is not None else anon.request(method, path)
            self.assertEqual(r.status_code, 401, (method, path))

    def test_every_change_is_written_to_the_log_without_passwords(self):
        self.add(); self.admin.patch("/api/inbox/team/mei", json={"password": "a brand new pass"}); self.admin.delete("/api/inbox/team/mei")
        actions = [c.kwargs["action"] for c in self.audit.await_args_list if c.kwargs.get("action", "").startswith("team_")]
        self.assertEqual(actions, ["team_member_added", "team_member_updated", "team_member_removed"])
        self.assertNotIn("password", str(self.audit.await_args_list).lower().replace("password_changed", ""))

    def test_team_page_for_the_admin(self):
        html = self.admin.get("/inbox/team").text
        for needle in ("Add team member", 'id="member-form"', "can only see the Inbox"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
