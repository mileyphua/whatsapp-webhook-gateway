"""Feedback -> lessons -> approved guidance loop (file backend, LLM faked, no network)."""
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


class LearningLoop(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close(); os.remove(self._tmp.name)
        fs._FILE = self._tmp.name
        learning.invalidate_cache()
        import main
        main.INBOX_ADMIN_TOKEN = main.INBOX_ADMIN_TOKEN or "t" * 20
        main._sb.ENABLED = False
        self.H = {"Authorization": "Bearer " + main.INBOX_ADMIN_TOKEN}
        self.c = TestClient(main.app)

    def tearDown(self):
        if os.path.exists(self._tmp.name):
            os.remove(self._tmp.name)
        learning.invalidate_cache()

    def test_full_loop(self):
        c, H = self.c, self.H
        body = {"e164": "60100000009", "ai_text": "Great question! Thanks for reaching out. Thanks for reaching out.",
                "buyer_text": "price VG30?", "rating": "down", "tags": ["Sounds like a bot", "Repeats itself"],
                "note": "Stop opening with Great question", "better_reply": "VG30 depends on volume. How many tonnes?"}
        self.assertEqual(c.post("/api/inbox/feedback", headers=H, json=body).status_code, 200)
        # re-rating the same reply overwrites rather than duplicating
        c.post("/api/inbox/feedback", headers=H, json=dict(body, note="Stop opening with Great question!!"))
        c.post("/api/inbox/feedback", headers=H, json={"e164": "60100000009", "ai_text": "VG30 needs a volume. How many tonnes?", "buyer_text": "price VG30?", "rating": "up"})
        self.assertEqual(c.post("/api/inbox/feedback", headers=H, json=dict(body, rating="meh")).status_code, 422)
        st = c.get("/api/inbox/learning", headers=H).json()["stats"]
        self.assertEqual((st["total"], st["up"], st["down"], st["approval_rate"]), (2, 1, 1, 50))

        # nothing changes in the prompt until a human approves a lesson
        before = asyncio.run(learning.guidance_block())
        fake = {"lessons": [{"text": "Never open with praise like 'Great question'; answer directly.", "kind": "style"},
                            {"text": "Check the knowledge base for: VG30 lead time", "kind": "knowledge"}]}
        with mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value=fake)):
            res = c.post("/api/inbox/learning/learn", headers=H).json()
        self.assertEqual((res["processed"], res["new_lessons"]), (1 + 1, 2))
        lessons = c.get("/api/inbox/learning", headers=H).json()["lessons"]
        self.assertTrue(all(l["status"] == "pending" for l in lessons))
        learning.invalidate_cache()
        self.assertNotIn("Never open with praise", asyncio.run(learning.guidance_block()))

        style = next(l for l in lessons if l["kind"] == "style")
        self.assertEqual(c.patch(f"/api/inbox/learning/lessons/{style['id']}", headers=H, json={"status": "active"}).status_code, 200)
        after = asyncio.run(learning.guidance_block())
        self.assertIn("Never open with praise", after)
        self.assertIn("How many tonnes?", after)          # approved better reply used as an example
        self.assertNotIn("knowledge base for: VG30", after)  # knowledge items never enter the prompt
        self.assertIn("Writing like a person", before)    # base rules always present

        # nothing left to learn => no LLM call
        with mock.patch.object(learning, "_llm_json", mock.AsyncMock(side_effect=AssertionError("should not call"))):
            self.assertEqual(c.post("/api/inbox/learning/learn", headers=H).json()["processed"], 0)

    def test_requires_auth(self):
        self.assertEqual(self.c.post("/api/inbox/feedback", json={}).status_code, 401)
        self.assertEqual(self.c.get("/api/inbox/learning").status_code, 401)


if __name__ == "__main__":
    unittest.main()
