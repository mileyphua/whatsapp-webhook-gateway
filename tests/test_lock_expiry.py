"""RED first. A Human lock must not keep the AI silent long after the person left: the page renews it every ~30s
while it is open, so it only needs to outlive a missed heartbeat or two."""
import os
import re
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import supabase_client as sb
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E = "60123456789"


class LockLifetime(unittest.TestCase):
    def test_the_page_asks_for_a_short_lock(self):
        js = open(os.path.join(ROOT, "petrobind_frontend_app", "static", "app.js"), encoding="utf-8").read()
        ttl = int(re.search(r"const HUMAN_TTL = (\d+);", js).group(1))
        self.assertLessEqual(ttl, 300, "AI stays silent this long after the person leaves")
        self.assertGreater(ttl, 60, "must survive a couple of missed 30s renewals")

    def test_the_server_caps_any_requested_lock_so_old_open_pages_cannot_silence_the_ai(self):
        import main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        sb._LOCAL_CLAIMS.clear()
        c = TestClient(main.app)
        c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        r = c.post(f"/api/inbox/chats/{E}/claim", json={"ttl_seconds": 1800})     # what an old open page still asks for
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(r.json()["expires_in_secs"], main.HUMAN_LOCK_MAX_SECONDS)
        self.assertLessEqual(main.HUMAN_LOCK_MAX_SECONDS, 300)
        self.assertLessEqual(sb._LOCAL_CLAIMS[E]["expires_at"] - __import__("time").time(), 301)
        sb._LOCAL_CLAIMS.clear()


if __name__ == "__main__":
    unittest.main()
