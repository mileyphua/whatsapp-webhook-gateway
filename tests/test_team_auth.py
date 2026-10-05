"""Slice 2 (RED first): one admin (the existing password) plus team members with their own username + password."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient


class _Base(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear()
        main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(main._refresh_users_cache())

    def tearDown(self):
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def client(self):
        return TestClient(self.main.app)

    def login(self, c, password, username=""):
        return c.post("/inbox/login", data={"username": username, "password": password}, follow_redirects=False)

    def me(self, c):
        r = c.get("/api/inbox/me")
        return r.status_code, (r.json() if r.status_code == 200 else None)


class Login(_Base):
    def test_admin_logs_in_with_only_the_admin_password(self):
        c = self.client(); r = self.login(c, "x" * 24)
        self.assertEqual((r.status_code, r.headers["location"]), (302, "/inbox/chats"))
        self.assertEqual(self.me(c), (200, {"role": "admin", "user": "admin", "name": self.main.INBOX_ADMIN_NAME}))

    def test_admin_can_also_type_the_username_admin(self):
        c = self.client(); self.login(c, "x" * 24, "Admin")
        self.assertEqual(self.me(c)[1]["role"], "admin")

    def test_team_member_logs_in_with_username_and_password(self):
        c = self.client(); r = self.login(c, "mei-password-1", "Mei")
        self.assertEqual(r.headers["location"], "/inbox/chats")
        self.assertEqual(self.me(c), (200, {"role": "agent", "user": "mei", "name": "Mei Ling"}))

    def test_wrong_unknown_and_cross_passwords_are_refused(self):
        for u, p in (("mei", "wrong-password"), ("nobody", "mei-password-1"), ("mei", "x" * 24), ("", "mei-password-1"), ("mei", "")):
            c = self.client(); r = self.login(c, p, u)
            self.assertIn("error=bad-password", r.headers["location"], (u, p))
            self.assertEqual(self.me(c)[0], 401, (u, p))

    def test_an_agent_cannot_use_the_admin_password_as_their_own(self):
        c = self.client(); r = self.login(c, "x" * 24, "mei")
        self.assertIn("error=", r.headers["location"])

    def test_disabled_member_cannot_log_in(self):
        asyncio.run(us.update_user("mei", disabled=True))
        c = self.client(); self.assertIn("error=", self.login(c, "mei-password-1", "mei").headers["location"])

    def test_disabling_cuts_off_a_session_that_is_already_open(self):
        c = self.client(); self.login(c, "mei-password-1", "mei")
        self.assertEqual(self.me(c)[0], 200)
        asyncio.run(us.update_user("mei", disabled=True)); asyncio.run(self.main._refresh_users_cache())
        self.assertEqual(self.me(c)[0], 401)

    def test_deleting_cuts_off_a_session_too(self):
        c = self.client(); self.login(c, "mei-password-1", "mei")
        asyncio.run(us.delete_user("mei")); asyncio.run(self.main._refresh_users_cache())
        self.assertEqual(self.me(c)[0], 401)

    def test_bearer_token_is_the_admin(self):
        r = self.client().get("/api/inbox/me", headers={"Authorization": "Bearer " + "x" * 24})
        self.assertEqual(r.json()["role"], "admin")

    def test_cookie_from_before_roles_existed_still_means_admin(self):
        payload = {"sid": "old-sid", "sub": "admin", "name": "Petrobind Admin", "iat": 1}
        c = self.client(); c.cookies.set("inbox_session", self.main._URL_SAFE_SERIALIZER.dumps(payload))
        self.assertEqual(self.me(c)[1]["role"], "admin")

    def test_a_forged_agent_cookie_for_a_user_that_does_not_exist_is_rejected(self):
        payload = {"sid": "s", "sub": "agent", "role": "agent", "user": "ghost", "name": "Ghost", "iat": 1}
        c = self.client(); c.cookies.set("inbox_session", self.main._URL_SAFE_SERIALIZER.dumps(payload))
        self.assertEqual(self.me(c)[0], 401)


class CookieCannotBeForged(_Base):
    """SECURITY: session cookies must be signed. (The signing key used to be read before it was defined, so signing
    silently switched itself off and a hand-made cookie {"sub":"admin"} opened the whole inbox without a password.)"""

    def test_cookie_signing_is_active(self):
        self.assertTrue(self.main._ITS_DANGEROUS_OK and self.main._URL_SAFE_SERIALIZER is not None)

    def _forged(self, **payload):
        import base64, json
        body = {"sid": "s", "sub": "admin", "name": "Mallory", "iat": 1}
        body.update(payload)
        return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")

    def test_a_hand_made_admin_cookie_is_rejected_everywhere(self):
        c = self.client(); c.cookies.set("inbox_session", self._forged())
        self.assertEqual(c.get("/api/inbox/me").status_code, 401)
        self.assertEqual(c.get("/api/inbox/chats").status_code, 401)
        r = c.get("/inbox/chats", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers.get("location", "").split("?")[0]), (302, "/inbox/login"))

    def test_a_hand_made_agent_cookie_is_rejected_too(self):
        c = self.client(); c.cookies.set("inbox_session", self._forged(sub="agent", role="agent", user="mei"))
        self.assertEqual(c.get("/api/inbox/me").status_code, 401)

    def test_a_cookie_with_a_tampered_role_is_rejected(self):
        c = self.client(); self.login(c, "mei-password-1", "mei")
        good = c.cookies.get("inbox_session")
        c.cookies.clear()                                   # replace the cookie, don't add a second one
        c.cookies.set("inbox_session", good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"))
        self.assertEqual(c.get("/api/inbox/me").status_code, 401)


class Throttle(_Base):
    def test_repeated_wrong_passwords_lock_that_login_for_a_while(self):
        c = self.client()
        for _ in range(5):
            self.assertIn("bad-password", self.login(c, "nope-nope-nope", "mei").headers["location"])
        r = self.login(c, "mei-password-1", "mei")          # even the right password is refused during the lockout
        self.assertIn("too-many", r.headers["location"])
        self.assertEqual(self.me(c)[0], 401)

    def test_lockout_ends_and_other_accounts_are_not_affected(self):
        c = self.client()
        for _ in range(5):
            self.login(c, "nope-nope-nope", "mei")
        self.assertEqual(self.login(self.client(), "x" * 24).headers["location"], "/inbox/chats")   # admin unaffected
        with mock.patch.object(self.main.time, "time", return_value=__import__("time").time() + 400):
            self.assertEqual(self.login(self.client(), "mei-password-1", "mei").headers["location"], "/inbox/chats")


if __name__ == "__main__":
    unittest.main()
