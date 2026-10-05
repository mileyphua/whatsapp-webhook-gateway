"""A person who is actively using the inbox must not be logged out mid-session (actions failed with 'Unauthorized')."""
import os
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

from fastapi.testclient import TestClient


class SessionRenewal(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear()
        self.c = TestClient(main.app)
        r = self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)

    def test_old_but_valid_session_is_renewed_when_used(self):
        real = self.main.time.time
        with mock.patch.object(self.main.time, "time", lambda: real() + 40 * 60):  # 40 of 60 minutes used
            r = self.c.get("/api/inbox/me")
            self.assertEqual(r.status_code, 200)
            self.assertIn("inbox_session=", r.headers.get("set-cookie", ""))
        with mock.patch.object(self.main.time, "time", lambda: real() + 80 * 60):  # past the ORIGINAL 60 minutes
            self.assertEqual(self.c.get("/api/inbox/me").status_code, 200)

    def test_fresh_session_is_not_reissued_every_request(self):
        r = self.c.get("/api/inbox/me")
        self.assertNotIn("inbox_session=", r.headers.get("set-cookie", ""))

    def test_expired_session_stays_rejected(self):
        real = self.main.time.time
        with mock.patch.object(self.main.time, "time", lambda: real() + 3 * 3600):
            self.assertEqual(self.c.get("/api/inbox/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
