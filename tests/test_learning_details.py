"""Clicking 'Learn from feedback' must show WHICH feedback was read and what came out of it, and must never mark
feedback as learned without actually reading it."""
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

PRICING = {"name": "Pricing questions", "always": False,
           "description": "Buyer asks how much something costs, for a quote, or to negotiate a price.",
           "instructions": "Ask for quantity and destination first, because price depends on both. Never quote a number yourself."}


class Base(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        fs._FILE = self._file = tmp.name
        learning.invalidate_cache()
        self._llm = mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value={"skills": [PRICING], "knowledge_checks": ["Check VG30 lead time"]}))
        self.llm = self._llm.start()
        import main
        main.INBOX_ADMIN_TOKEN = main.INBOX_ADMIN_TOKEN or "t" * 20
        main._sb.ENABLED = False
        self.H = {"Authorization": "Bearer " + main.INBOX_ADMIN_TOKEN}
        self.c = TestClient(main.app)

    def tearDown(self):
        self._llm.stop()
        if os.path.exists(self._file): os.remove(self._file)
        learning.invalidate_cache()

    def rate(self, n, rating="down", note="Don't quote prices"):
        asyncio.run(fs.add_feedback(e164="60100000009", ai_text=f"Reply number {n}: the price is $500.", buyer_text=f"price question {n}?",
                                    rating=rating, note=note, better_reply="Depends on volume.", actor="Admin"))


class WhatWasLearned(Base):
    def test_the_result_lists_every_feedback_that_was_read(self):
        for n in (1, 2, 3): self.rate(n)
        out = asyncio.run(learning.distill())
        self.assertEqual(out["processed"], 3)
        self.assertEqual(sorted(i["buyer_text"] for i in out["items"]), ["price question 1?", "price question 2?", "price question 3?"])
        one = out["items"][0]
        for k in ("id", "e164", "rating", "buyer_text", "ai_text", "note", "better_reply", "ts"):
            self.assertIn(k, one)

    def test_the_result_names_the_skills_and_knowledge_that_came_out(self):
        self.rate(1)
        out = asyncio.run(learning.distill())
        self.assertEqual([(s["name"], s["action"]) for s in out["skills_touched"]], [("Pricing questions", "new")])
        self.assertEqual(out["knowledge"], ["Check VG30 lead time"])

    def test_an_update_to_an_existing_skill_is_reported_as_an_update(self):
        self.rate(1); asyncio.run(learning.distill())
        revised = {**PRICING, "instructions": "Ask for quantity, destination and timing first, because price depends on all three."}
        self.llm.return_value = {"skills": [revised], "knowledge_checks": []}
        self.rate(2)
        out = asyncio.run(learning.distill())
        self.assertEqual([(s["name"], s["action"]) for s in out["skills_touched"]], [("Pricing questions", "updated")])

    def test_nothing_waiting_says_so_and_lists_nothing(self):
        out = asyncio.run(learning.distill())
        self.assertEqual((out["processed"], out["items"]), (0, []))

    def test_feedback_beyond_the_first_40_is_not_marked_learned_unread(self):
        for n in range(45): self.rate(n)
        out = asyncio.run(learning.distill())
        self.assertEqual((out["processed"], len(out["items"]), out["remaining"]), (40, 40, 5))
        waiting = [i for i in asyncio.run(fs.list_feedback()) if not i.get("processed")]
        self.assertEqual(len(waiting), 5)
        out2 = asyncio.run(learning.distill())
        self.assertEqual((out2["processed"], out2["remaining"]), (5, 0))


class LearningPageData(Base):
    def data(self):
        return self.c.get("/api/inbox/learning", headers=self.H).json()

    def test_waiting_feedback_is_listed_before_learning(self):
        self.rate(1); self.rate(2, "up", note="")
        d = self.data()
        self.assertEqual(sorted(w["buyer_text"] for w in d["waiting"]), ["price question 1?", "price question 2?"])
        asyncio.run(learning.distill())
        self.assertEqual(self.data()["waiting"], [])

    def test_recent_feedback_shows_which_rows_were_learned(self):
        self.rate(1); asyncio.run(learning.distill()); self.rate(2)
        rows = {r["buyer_text"]: r["processed"] for r in self.data()["recent_feedback"]}
        self.assertEqual(rows, {"price question 1?": True, "price question 2?": False})

    def test_each_learned_skill_shows_the_feedback_it_came_from(self):
        self.rate(1); self.rate(2)
        asyncio.run(learning.distill())
        skill = [s for s in self.data()["skills"] if s["name"] == "Pricing questions"][0]
        self.assertEqual(skill["source_count"], 2)
        self.assertEqual(sorted(x["buyer_text"] for x in skill["sources"]), ["price question 1?", "price question 2?"])
        self.assertIn("note", skill["sources"][0])

    def test_the_learn_button_endpoint_returns_the_details(self):
        self.rate(1)
        r = self.c.post("/api/inbox/learning/learn", headers=self.H).json()
        self.assertEqual((r["processed"], len(r["items"]), r["skills_touched"][0]["name"]), (1, 1, "Pricing questions"))


if __name__ == "__main__":
    unittest.main()
