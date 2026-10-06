"""Spec answers cover every property in the knowledge base, end with an engaging 'would you like to know more?', and the
website page is sent only when the buyer says they do (not pasted into the first answer)."""
import asyncio
import json
import os
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import ai_health
import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
import importlib
R = importlib.import_module("rag.retrieve")          # (the package re-exports a function called retrieve)
from rag.retrieve import RetrievedChunk

URL = "https://www.petrobindglobal.com/products/bitumen-60-70"
CHUNK0 = RetrievedChunk(url=URL, title="Bitumen 60/70 Supplier | COA/PDS | PetroBind Global", chunk_text="Title: Bitumen 60/70\nPENETRATION 60-70 dmm", similarity=0.9, chunk_index=0)
PH = "60177700066"


class Base(unittest.TestCase):
    def setUp(self):
        ai_health.reset()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_km_skills.json"; learning.invalidate_cache()
        R.load_index_if_needed()
        self.llm_calls, self.systems = 0, []

    def turn(self, text, reply, *, session=None, refs=None, message_count=3):
        plan = json.dumps({"intent": "x", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})
        seq = [plan, reply]; n = {"i": 0}

        async def create(**kw):
            self.llm_calls += 1
            self.systems.append("\n".join(m["content"] for m in kw.get("messages", []) if m.get("role") == "system"))
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def retrieve(q, **kw): return refs if refs is not None else [CHUNK0]

        if session is None:
            cs._SESSIONS.pop(PH, None)
            session = cs.ConversationSession(phone_number=PH); session.message_count = message_count
            session.history += [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi, how can I help?"}]
        cs._SESSIONS[PH] = session
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", retrieve), \
                mock.patch.object(L, "index_is_ready", lambda: True):
            return asyncio.run(L.handle_incoming_message(phone_number=PH, inbound_text=text, draft_only=False)), cs._SESSIONS[PH]


SPEC_REPLY = ("Typical specs for Bitumen 60/70:\nPenetration at 25 C: 60-70 dmm\nSoftening point: 49-56 C\nDuctility at 25 C: min 100 cm\n"
              "Flash point: min 250 C\nSolubility: min 99.0%")


class CompleteSpecs(Base):
    def test_a_spec_request_gives_the_ai_the_whole_page_not_just_the_best_chunk(self):
        self.turn("what are the specs of bitumen 60/70?", SPEC_REPLY)
        prompt = self.systems[-1]
        for needle in ("Softening Point (Ring & Ball)", "Flash Point (Cleveland Open Cup)", "Solubility in trichloroethylene", "Spot test", "Loss on heating"):
            self.assertIn(needle, prompt)

    def test_the_prompt_demands_every_property_one_per_line(self):
        self.turn("specs of 60/70?", SPEC_REPLY)
        p = " ".join(self.systems[-1].split()).lower()
        self.assertIn("every property", p); self.assertIn("one per line", p)
        self.assertIn("do not paste a website link", p)

    def test_spec_lines_survive_cleaning_as_separate_lines(self):
        out, _ = self.turn("specs of 60/70?", SPEC_REPLY)
        for line in ("Penetration at 25 C: 60-70 dmm", "Softening point: 49-56 C", "Flash point: min 250 C"):
            self.assertIn(line, out)
        self.assertGreaterEqual(out.count("\n"), 4)


class EngagingQuestionThenLink(Base):
    def test_the_answer_ends_with_a_would_you_like_to_know_more_question_and_no_link(self):
        out, s = self.turn("what are the specs of bitumen 60/70?", SPEC_REPLY)
        self.assertTrue(out.strip().endswith("?")); self.assertIn("know more about Bitumen 60/70", out)
        self.assertNotIn("petrobindglobal.com", out)
        self.assertEqual(s.more_info_url, URL)

    def test_a_link_the_model_pasted_is_withheld_until_the_buyer_asks(self):
        out, s = self.turn("tell me more about 60/70", f"It is a paving grade for roads.\n\nMore details here: {URL}")
        self.assertNotIn("petrobindglobal.com", out); self.assertNotIn("More details here", out)
        self.assertIn("paving grade", out)

    def test_a_buyer_who_asks_for_the_link_gets_it(self):
        out, _ = self.turn("what is the website link for 60/70?", f"Here it is: {URL}")
        self.assertIn(URL, out)

    def test_yes_sends_the_page_without_calling_the_ai(self):
        _, s = self.turn("what are the specs of bitumen 60/70?", SPEC_REPLY)
        self.llm_calls = 0
        for yes in ("yes", "Yes please", "sure", "ok", "tell me more", "send me the link", "yeah go ahead"):
            s.more_info_url, s.more_info_title = URL, "Bitumen 60/70"
            import time as _t; s.more_info_at = _t.time()
            out, s = self.turn(yes, "SHOULD NOT BE USED", session=s)
            self.assertIn(URL, out, yes); self.assertIn("Bitumen 60/70", out)
            self.assertEqual(self.llm_calls, 0, yes); self.assertEqual(s.more_info_url, "")

    def test_no_and_unrelated_messages_do_not_send_the_page(self):
        for text in ("no thanks", "not now", "what is the price?", "I want 500 tonnes to Port Klang"):
            _, s = self.turn("what are the specs of bitumen 60/70?", SPEC_REPLY)
            out, _ = self.turn(text, "Fine.", session=s)
            self.assertNotIn(URL, out, text)

    def test_yes_with_nothing_pending_is_just_a_normal_turn(self):
        out, _ = self.turn("yes", "Great, which grade do you need?")
        self.assertNotIn("petrobindglobal.com", out); self.assertGreaterEqual(self.llm_calls, 1)

    def test_an_old_offer_expires(self):
        _, s = self.turn("what are the specs of bitumen 60/70?", SPEC_REPLY)
        s.more_info_at -= 3 * 3600
        out, _ = self.turn("yes", "Sure, what do you need?", session=s)
        self.assertNotIn(URL, out)

    def test_price_and_booking_replies_do_not_leave_an_offer_behind(self):
        _, s = self.turn("how much is 60/70?", "Hold on, let me check with my sales director about the latest price to confirm.")
        self.assertEqual(s.more_info_url, "")


if __name__ == "__main__":
    unittest.main()
