"""RED first. The 'don't repeat yourself' filter must never delete the sentence that ANSWERS the buyer's question.
Bug: the buyer asked 'who are you?' twice; the (correct) answer matched an earlier reply, was stripped as a repeat, and
only 'How can I help with your bitumen inquiry today?' was left, so the question was never answered."""
import asyncio
import json
import os
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""
os.environ.setdefault("OPENROUTER_API_KEY", "test")

import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
import reply_guard as g

EARLIER = ["Hi, I'm Jane, Petrobind Global's Sales Assistant on WhatsApp."]
NEW = "I'm Jane, Petrobind Global's Sales Assistant on WhatsApp. How can I help with your bitumen inquiry today?"


class StripRepeats(unittest.TestCase):
    def test_the_answer_to_a_question_survives_even_if_said_before(self):
        out = g.strip_repeats(NEW, EARLIER, buyer_text="who are you?")
        self.assertIn("I'm Jane", out)

    def test_a_re_asked_question_gets_its_full_answer_again(self):
        out = g.strip_repeats(NEW, EARLIER, buyer_text="Hi, can I ask who are you?", previous_buyer_texts=["who are you?"])
        self.assertEqual(out, NEW)

    def test_repeated_pleasantries_are_still_removed_when_nothing_was_asked(self):
        prev = ["Thanks for reaching out. We supply Bitumen 60/70 in new steel drums."]
        out = g.strip_repeats("Thanks for reaching out. Lead time is about two weeks.", prev, buyer_text="ok thanks")
        self.assertEqual(out, "Lead time is about two weeks.")

    def test_later_repeated_sentences_are_still_removed_even_for_a_question(self):
        prev = ["We supply Bitumen 60/70 in new steel drums. Our minimum order is one container."]
        out = g.strip_repeats("Lead time is two weeks. Our minimum order is one container.", prev, buyer_text="how long does it take?")
        self.assertEqual(out, "Lead time is two weeks.")

    def test_question_detection(self):
        for t in ("who are you?", "Hi, can I ask who are you", "what do you supply", "do you ship to Dubai", "tell me about 60/70"):
            self.assertTrue(g.is_question(t), t)
        for t in ("ok thanks", "20 tonnes to Port Klang", "hello"):
            self.assertFalse(g.is_question(t), t)


class PipelineAnswersWhoAreYou(unittest.TestCase):
    def test_who_are_you_is_answered_even_when_the_same_answer_was_given_before(self):
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_who_skills.json"; learning.invalidate_cache()
        plan = json.dumps({"intent": "asks who we are", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "friendly"})
        seq = [plan, NEW]; n = {"i": 0}

        async def create(**kw):
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def no_refs(_q):
            return []

        s = cs.ConversationSession(phone_number="60177700099")
        s.append("user", "Hi, can I ask who are you?"); s.append("assistant", EARLIER[0])
        cs._SESSIONS["60177700099"] = s
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", no_refs), mock.patch.object(L, "index_is_ready", lambda: False):
            out = asyncio.run(L.handle_incoming_message(phone_number="60177700099", inbound_text="who are you?"))
        cs._SESSIONS.pop("60177700099", None)
        self.assertIn("I'm Jane", out)


if __name__ == "__main__":
    unittest.main()


class GenericInquiryRecapIsDropped(unittest.TestCase):
    """The buyer only asked who we are; the reply must not drag the old inquiry back in."""

    def test_recap_style_closing_questions_are_removed_when_the_buyer_did_not_raise_orders(self):
        for recap in ("How can I help with your bitumen inquiry today?",
                      "What can I help you with today regarding Bitumen 60/70?",
                      "Can I assist you with your Bitumen 60/70 order today?"):
            out = g.strip_order_reminders("I'm Jane, Petrobind Global's Sales Assistant on WhatsApp. " + recap, "who are you?")
            self.assertEqual(out, "I'm Jane, Petrobind Global's Sales Assistant on WhatsApp.", recap)

    def test_they_are_kept_when_the_buyer_is_talking_about_their_order(self):
        reply = "How can I help with your bitumen inquiry today?"
        self.assertEqual(g.strip_order_reminders(reply, "any update on my order?"), reply)

    def test_ordinary_questions_in_an_active_flow_are_untouched(self):
        for q in ("Which port should it go to?", "What quantity are you looking at?", "How can I help you today?", "What can I help you with today?"):
            self.assertEqual(g.strip_order_reminders("Got it. " + q, "ok"), "Got it. " + q, q)
