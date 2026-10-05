"""Feedback -> skills loop (file backend, LLM faked, no network)."""
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
           "instructions": "Ask for quantity and destination first, because price depends on both. Never quote a number yourself; hand over to a colleague."}


class SkillsLoop(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        fs._FILE = self._file = tmp.name
        learning.invalidate_cache()
        import main
        main.INBOX_ADMIN_TOKEN = main.INBOX_ADMIN_TOKEN or "t" * 20
        main._sb.ENABLED = False
        self.H = {"Authorization": "Bearer " + main.INBOX_ADMIN_TOKEN}
        self.c = TestClient(main.app)

    def tearDown(self):
        if os.path.exists(self._file):
            os.remove(self._file)
        learning.invalidate_cache()

    def _rate(self, **kw):
        body = dict({"e164": "60100000009", "ai_text": "Great question! The price is $500.", "buyer_text": "price VG30?", "rating": "down",
                     "tags": ["Sounds like a bot"], "note": "Don't quote prices", "better_reply": "Depends on volume. How many tonnes and where to?"}, **kw)
        self.assertEqual(self.c.post("/api/inbox/feedback", headers=self.H, json=body).status_code, 200)

    def _skills(self):
        return self.c.get("/api/inbox/learning", headers=self.H).json()["skills"]

    def test_feedback_becomes_an_approved_skill_used_only_when_it_fits(self):
        c, H = self.c, self.H
        self._rate()
        self._rate(ai_text="VG30 needs a volume first. How many tonnes?", rating="up", better_reply="", note="")
        st = c.get("/api/inbox/learning", headers=H).json()["stats"]
        self.assertEqual((st["total"], st["up"], st["down"], st["approval_rate"]), (2, 1, 1, 50))

        fake = {"skills": [PRICING], "knowledge_checks": ["Check the knowledge base for: VG30 lead time"]}
        with mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value=fake)):
            res = c.post("/api/inbox/learning/learn", headers=H).json()
        self.assertEqual((res["processed"], res["new_skills"], res["updated_skills"]), (2, 1, 0))
        learned = [s for s in self._skills() if not s.get("builtin")]
        self.assertEqual([s["status"] for s in learned], ["pending"])

        # pending skills do nothing to the prompt
        skills = asyncio.run(learning.active_skills())
        self.assertNotIn("Pricing questions", [s["name"] for s in skills])

        sid = learned[0]["id"]
        self.assertEqual(c.patch(f"/api/inbox/learning/skills/{sid}", headers=H, json={"status": "active"}).status_code, 200)
        skills = asyncio.run(learning.active_skills())
        ids = [s["id"] for s in skills]
        self.assertIn(sid, ids)
        # only loaded when selected; the always-on voice skill is always there
        with_it = learning.build_guidance(skills, [sid])
        without = learning.build_guidance(skills, [])
        self.assertIn("Ask for quantity and destination first", with_it)
        self.assertNotIn("Ask for quantity and destination first", without)
        self.assertIn("Sound like a person", without)
        self.assertIn("How many tonnes and where to?", with_it)      # approved better reply used as an example
        # catalog shown to the planner lists the WHEN of non-always skills only
        cat = learning.catalog_text(skills)
        self.assertIn("Buyer asks how much", cat)
        self.assertNotIn("Sound like a person", cat)
        # knowledge checks are surfaced separately and never injected
        know = c.get("/api/inbox/learning", headers=H).json()["knowledge"]
        self.assertEqual(len(know), 1)
        self.assertNotIn("VG30 lead time", with_it)

        # more feedback on the same situation proposes a REVISION, applied only after approval
        self._rate(ai_text="Sure. About $480 per tonne.", buyer_text="how much for 100t?", note="Also never quote numbers")
        revised = dict(PRICING, instructions="Ask for quantity and destination first. Never quote a number; say a colleague will confirm.")
        with mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value={"skills": [revised], "knowledge_checks": []})):
            res = c.post("/api/inbox/learning/learn", headers=H).json()
        self.assertEqual((res["new_skills"], res["updated_skills"]), (0, 1))
        s = next(x for x in self._skills() if x["id"] == sid)
        self.assertIn("say a colleague will confirm", s["proposal"]["instructions"])
        self.assertNotIn("say a colleague will confirm", s["instructions"])
        c.patch(f"/api/inbox/learning/skills/{sid}", headers=H, json={"proposal": "approve"})
        s = next(x for x in self._skills() if x["id"] == sid)
        self.assertIsNone(s["proposal"]); self.assertIn("say a colleague will confirm", s["instructions"])

    def test_manual_skill_and_builtin_protection(self):
        c, H = self.c, self.H
        r = c.post("/api/inbox/learning/skills", headers=H, json={"name": "Shipping", "description": "Buyer asks about delivery time", "instructions": "Ask the port first because lead time depends on it."})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["skill"]["status"], "active")           # a human wrote it, so it is live
        self.assertEqual(c.post("/api/inbox/learning/skills", headers=H, json={"name": "x"}).status_code, 422)
        for call in (lambda: c.patch("/api/inbox/learning/skills/builtin-hand-over", headers=H, json={"status": "disabled"}),
                     lambda: c.delete("/api/inbox/learning/skills/builtin-human-voice", headers=H)):
            self.assertEqual(call().status_code, 400)

    def test_selection_fallback_and_planner_validation(self):
        skills = learning.BUILTIN_SKILLS + [dict(PRICING, id="p1", status="active")]
        self.assertEqual(learning.select_without_planner(skills, "can you give me a quote for the price?")[:1], ["p1"])
        self.assertEqual(learning.select_without_planner(skills, "hello"), [])
        fake = {"intent": "price", "needs_human": False, "skills": ["p1", "made-up-id"], "points": ["ask quantity"], "avoid": []}
        with mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value=fake)):
            plan = asyncio.run(learning.plan_reply(buyer_text="how much", recent=[], ref_titles=[], skills=skills))
        self.assertEqual(plan["skills"], ["p1"])   # unknown ids are dropped

    def test_requires_auth(self):
        self.assertEqual(self.c.post("/api/inbox/feedback", json={}).status_code, 401)
        self.assertEqual(self.c.get("/api/inbox/learning").status_code, 401)
        self.assertEqual(self.c.post("/api/inbox/learning/skills", json={}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
