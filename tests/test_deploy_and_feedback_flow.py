"""RED first: browsers must always revalidate app assets, a bad rating with a reason must start learning
immediately, and two learning runs must never overlap."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import feedback_store as fs
import learning
from fastapi.testclient import TestClient


class _Base(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        fs._FILE = self._file = tmp.name
        learning.invalidate_cache()
        self._no_net = mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value=None))
        self._no_net.start()
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = main.INBOX_ADMIN_TOKEN or "t" * 20
        main._sb.ENABLED = False
        self.H = {"Authorization": "Bearer " + main.INBOX_ADMIN_TOKEN}
        self.c = TestClient(main.app)

    def tearDown(self):
        self._no_net.stop()
        if os.path.exists(self._file):
            os.remove(self._file)


class AssetFreshness(_Base):
    def test_static_assets_must_be_revalidated(self):
        """A browser that cached an older app.js can't show new buttons (e.g. the rating row)."""
        for path in ("/inbox/static/app.js", "/static/admin.js"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("no-cache", r.headers.get("cache-control", ""), path)

    def test_pages_reference_versioned_assets(self):
        c2 = TestClient(self.main.app)
        c2.post("/inbox/login", data={"password": self.main.INBOX_ADMIN_TOKEN}, follow_redirects=False)
        html = c2.get("/inbox/learning").text
        self.assertRegex(html, r'/inbox/static/app\.js\?v=\w+')


class FeedbackStartsLearning(_Base):
    BODY = {"e164": "60100000009", "ai_text": "Great question! Price is $500.", "buyer_text": "price?", "rating": "down"}

    def test_bad_rating_with_a_reason_starts_learning_now(self):
        """The admin said why => don't make them wait for 5 ratings or press a button."""
        with mock.patch.object(learning, "distill", mock.AsyncMock(return_value={"ok": True})) as d:
            r = self.c.post("/api/inbox/feedback", headers=self.H, json=dict(self.BODY, note="Never quote prices"))
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["learning_started"])
            self.assertEqual(d.await_count, 1)

    def test_good_rating_or_bare_thumbs_down_does_not_trigger_learning(self):
        with mock.patch.object(learning, "distill", mock.AsyncMock(return_value={"ok": True})) as d:
            r1 = self.c.post("/api/inbox/feedback", headers=self.H, json=dict(self.BODY, rating="up"))
            r2 = self.c.post("/api/inbox/feedback", headers=self.H, json=dict(self.BODY, ai_text="another"))
            self.assertFalse(r1.json()["learning_started"]); self.assertFalse(r2.json()["learning_started"])
            self.assertEqual(d.await_count, 0)


class DistillIsSingleFlight(_Base):
    def test_overlapping_runs_call_the_model_once(self):
        """Several quick ratings must not create duplicate skills."""
        asyncio.run(fs.add_feedback(e164="1", ai_text="a", buyer_text="b", rating="down", note="n"))
        calls = {"n": 0}

        async def slow(*a, **k):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return {"skills": [], "knowledge_checks": []}

        async def both():
            with mock.patch.object(learning, "_llm_json", slow):
                return await asyncio.gather(learning.distill(), learning.distill())

        res = asyncio.run(both())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sorted(bool(r.get("busy")) for r in res), [False, True])


if __name__ == "__main__":
    unittest.main()
