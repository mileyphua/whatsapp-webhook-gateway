"""The Ops page buttons 'Run follow-ups scan' and 'Flush scheduled sends' must run the scan inside the live app.
They used to open a nested test client, which re-ran the whole startup sequence (reload the knowledge index, start
another background loop) on every click, and failed outright whenever startup raised."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient


class OpsButtons(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear()
        self.startups = []

        async def spy_loop(): self.startups.append("loop")

        async def boom_startup(): self.startups.append("warm"); raise RuntimeError("startup must not run again")

        self._p = [mock.patch.object(main, "FOLLOWUPS_CRON_TOKEN", "cron-secret-token-123"),
                   mock.patch.object(main, "_users_cache_loop", spy_loop),
                   mock.patch.object(main, "_refresh_users_cache", mock.AsyncMock())]
        for p in self._p: p.start()
        # the handlers registered at import time are what a nested TestClient would re-run
        self._startup = list(main.app.router.on_startup)
        main.app.router.on_startup[:] = [boom_startup]
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        self.main.app.router.on_startup[:] = self._startup
        for p in self._p: p.stop()
        if os.path.exists(self._file): os.remove(self._file)

    def test_followups_scan_button_runs_without_restarting_the_app(self):
        r = self.c.post("/api/inbox/admin/run-followups-scan")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"]); self.assertIn("followups_scan_result", r.json())
        self.assertEqual(self.startups, [])

    def test_flush_button_runs_without_restarting_the_app(self):
        r = self.c.post("/api/inbox/admin/flush-scheduled-sends")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"]); self.assertIn("flush_result", r.json())
        self.assertEqual(self.startups, [])

    def test_the_cron_endpoints_still_need_the_token(self):
        self.assertEqual(self.c.post("/followups-scan").status_code, 401)
        self.assertEqual(self.c.post("/followups-scan", headers={"Authorization": "Bearer cron-secret-token-123"}).status_code, 200)

    def test_buttons_are_admin_only(self):
        self.assertEqual(TestClient(self.main.app).post("/api/inbox/admin/run-followups-scan").status_code, 401)


if __name__ == "__main__":
    unittest.main()
