"""(1) The 'Hi, this is Jane...' introduction belongs to the very start of a chat only.
(2) A buyer asking for specs gets the specs first, never a bare 'let me ask the sales director'; a price+spec message
gets the specs before the pricing line."""
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
from rag.retrieve import RetrievedChunk

CHUNK = RetrievedChunk(url="https://www.petrobindglobal.com/products/bitumen-60-70", title="Bitumen 60/70",
                       chunk_text="Bitumen 60/70: penetration 60-70 dmm, softening point 49-56 C.", similarity=0.9, chunk_index=0)
GREETING = "Hi 👋 this is Jane from PetroBind Global. How can we help you with your requirement today?"
PRICING_LINE = "Hold on, let me check with my sales director about the latest price to confirm."
PH = "60177700055"


class Base(unittest.TestCase):
    def setUp(self):
        ai_health.reset()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_specs_skills.json"; learning.invalidate_cache()
        self.queries, self.emails, self.llm_calls = [], [], 0

    def turn(self, text, reply, *, history=None, message_count=0, product="", refs=None, draft_only=False):
        plan = json.dumps({"intent": "x", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})
        seq = [plan, reply]; n = {"i": 0}

        async def create(**kw):
            self.llm_calls += 1
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def retrieve(q):
            self.queries.append(q)
            return refs if refs is not None else [CHUNK]

        async def email(**kw): self.emails.append(kw); return True

        cs._SESSIONS.pop(PH, None)
        s = cs.ConversationSession(phone_number=PH); s.message_count = message_count
        for m in history or []: s.history.append(m)
        if product: s.inquiry.product = product
        cs._SESSIONS[PH] = s
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", retrieve), \
                mock.patch.object(L, "index_is_ready", lambda: True), mock.patch.object(L.notify, "send_handoff_email", email), \
                mock.patch.object(L.booking, "is_configured", lambda: True):
            return asyncio.run(L.handle_incoming_message(phone_number=PH, inbound_text=text, draft_only=draft_only)), s


MID_CHAT = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": GREETING}]


class IntroductionOnlyAtTheStart(Base):
    def test_the_first_reply_of_a_chat_keeps_the_introduction(self):
        out, _ = self.turn("hi", GREETING)
        self.assertIn("this is Jane", out)

    def test_later_replies_never_repeat_the_introduction(self):
        out, _ = self.turn("what is bitumen emulsion used for?", GREETING + "\n\nIt is used for tack coats and chip seals.", history=MID_CHAT, message_count=1)
        self.assertNotIn("this is Jane", out); self.assertNotIn("How can we help you with your requirement", out)
        self.assertIn("tack coats", out)

    def test_hi_in_the_middle_of_a_chat_gets_a_short_hello_not_the_introduction(self):
        out, _ = self.turn("hi again", GREETING, history=MID_CHAT + [{"role": "user", "content": "what is emulsion?"}, {"role": "assistant", "content": "Used for tack coats."}], message_count=2)
        self.assertNotIn("this is Jane", out); self.assertNotIn("requirement today", out)
        self.assertTrue(out.strip()); self.assertLess(len(out), 80)

    def test_the_introduction_is_removed_wherever_it_sits_in_a_later_reply(self):
        out, _ = self.turn("is it good for roads?", "Yes, it is a paving grade.\n\nHi 👋 this is Jane from PetroBind Global.", history=MID_CHAT, message_count=1)
        self.assertNotIn("this is Jane", out); self.assertIn("paving grade", out)


class SpecsAreAnsweredFirst(Base):
    def test_a_vague_spec_request_asks_which_product_and_links_the_products_page(self):
        out, s = self.turn("I need to know more about the specs first", "unused", refs=[], history=MID_CHAT, message_count=1)
        low = out.lower()
        self.assertIn("which product", low); self.assertIn("https://www.petrobindglobal.com/products", out)
        for bad in ("sales director", "check on that", "quick call", "cal.com", "get back to you"):
            self.assertNotIn(bad, low)
        self.assertIsNone(s.needs_human_since); self.assertEqual(self.emails, [])

    def test_the_known_product_is_used_to_find_the_specs(self):
        self.turn("I need to know more about the specs first", "The specs: penetration 60-70 dmm.", refs=[CHUNK], history=MID_CHAT, message_count=1, product="Bitumen 60/70")
        self.assertTrue(any("60/70" in q for q in self.queries), self.queries)

    def test_a_specific_spec_question_with_no_match_still_goes_to_a_person_not_a_guess(self):
        out, s = self.turn("can you send me the spec sheet for VG30?", "unused", refs=[], history=MID_CHAT, message_count=1)
        self.assertIn("get back to you", out.lower()); self.assertIsNotNone(s.needs_human_since)

    def test_specs_come_before_the_pricing_line_when_both_are_asked(self):
        reply = f"{PRICING_LINE}\n\nBitumen 60/70: penetration 60-70 dmm, softening point 49-56 C.\n\nWhich destination port do you need?"
        out, _ = self.turn("what are the specs of 60/70 and the price?", reply)
        self.assertLess(out.index("penetration 60-70"), out.index("sales director"))
        self.assertLess(out.index("sales director"), out.index("Which destination port"))

    def test_a_reply_that_is_only_the_pricing_line_is_left_alone(self):
        out, _ = self.turn("how much is 60/70?", PRICING_LINE)
        self.assertEqual(out.strip(), PRICING_LINE)

    def test_the_prompts_tell_the_ai_to_answer_specs_itself(self):
        prompt = " ".join(L.COMPANY_PROFILE.split()).lower()
        self.assertIn("specs first", prompt)
        self.assertIn("never answer a spec question with only", prompt)
        planner = " ".join(learning._PLANNER_SYSTEM.split()).lower()
        self.assertIn("spec", planner); self.assertIn("not a reason", planner)


if __name__ == "__main__":
    unittest.main()
