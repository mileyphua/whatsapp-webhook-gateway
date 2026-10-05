"""Slice 6 (RED first): the pages say the right thing to the right person."""
import asyncio
import os
import tempfile
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def js():
    return open(os.path.join(ROOT, "petrobind_frontend_app", "static", "app.js"), encoding="utf-8").read()


class UiForTeam(unittest.TestCase):
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

    def tearDown(self):
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def login_as(self, username, password):
        c = TestClient(self.main.app)
        c.post("/inbox/login", data={"username": username, "password": password}, follow_redirects=False)
        return c

    def test_login_page_asks_for_username_and_password(self):
        html = TestClient(self.main.app).get("/inbox/login").text
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertNotIn("INBOX_ADMIN_TOKEN", html)               # no server setup hints on a public page
        self.assertNotIn("secrets.token_urlsafe", html)

    def test_login_errors_are_plain_language(self):
        c = TestClient(self.main.app)
        self.assertIn("Incorrect username or password", c.get("/inbox/login?error=bad-password").text)
        self.assertIn("Too many attempts", c.get("/inbox/login?error=too-many").text)

    def test_thread_page_tells_the_script_who_is_viewing(self):
        html = self.login_as("mei", "mei-password-1").get("/inbox/chat/60123456789").text
        self.assertIn('"is_admin": false', html.replace("is_admin:", '"is_admin":').replace(" false", " false")) if False else self.assertRegex(html, r"is_admin:\s*false")
        admin = self.login_as("", "x" * 24).get("/inbox/chat/60123456789").text
        self.assertRegex(admin, r"is_admin:\s*true")

    def test_thread_has_a_release_lock_button_for_the_admin(self):
        html = self.login_as("", "x" * 24).get("/inbox/chat/60123456789").text
        self.assertIn('id="release-lock"', html)

    def test_script_force_releases_and_names_the_person(self):
        src = js()
        self.assertIn("claim?force=1", src)
        self.assertRegex(src, r"is replying to this chat")
        self.assertNotIn("Another admin", src, "there is one admin; say who is replying instead")


if __name__ == "__main__":
    unittest.main()
