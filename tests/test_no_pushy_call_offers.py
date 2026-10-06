"""When a buyer asks for information the AI answers from the knowledge base and points to the website page; it must not
keep pushing a quote request / call / sales director. Those offers are only for a buyer who wants to proceed."""
import asyncio
import json
import os
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
from rag.retrieve import RetrievedChunk

CHUNK = RetrievedChunk(url="https://www.petrobindglobal.com/products/bitumen-60-70", title="Bitumen 60/70",
                       chunk_text="Bitumen 60/70 is a paving grade used for roads.", similarity=0.9, chunk_index=0)

PHRASES = ("Quick question", "put together a quote", "share a time slot", "pass you to our sales director")


class Base(unittest.TestCase):
    def turn(self, buyer_text, llm_reply, qa_count=0, refs=None, draft_only=False, client_override=None):
        refs = refs if refs is not None else [CHUNK]
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_pushy_skills.json"; learning.invalidate_cache()
        plan = json.dumps({"intent": "information", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})
        seq = [plan, llm_reply]; n = {"i": 0}; self.sent_messages = []

        async def create(**kw):
            self.sent_messages.append(kw.get("messages"))
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def get_refs(_q):
            return refs or []

        cs._SESSIONS.pop("60177700009", None)
        s = cs.ConversationSession(phone_number="60177700009"); s.message_count = 5; s.freeform_questions_answered = qa_count
        cs._SESSIONS["60177700009"] = s
        client = client_override or client
        self.cal = "https://cal.com/petrobindglobal/30min"
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", get_refs), mock.patch.object(L, "index_is_ready", lambda: True), \
                mock.patch.object(L.booking, "is_configured", lambda: True), mock.patch.object(L, "_booking_link_for", lambda *a, **k: self.cal):
            return asyncio.run(L.handle_incoming_message(phone_number="60177700009", inbound_text=buyer_text, draft_only=draft_only))


class NoQuickQuestionNudge(Base):
    def test_the_reply_is_never_followed_by_the_quick_question(self):
        for qa in (0, 3, 6, 10):
            out = self.turn("what is emulsion used for?", "Bitumen emulsion is used for tack coats and chip seals.", qa_count=qa)
            for p in PHRASES:
                self.assertNotIn(p.lower(), out.lower(), f"qa_count={qa}")
            self.assertIn("tack coats", out)

    def test_the_nudge_code_is_gone(self):
        self.assertFalse(hasattr(L, "_nudge_if_needed"))
        src = open(L.__file__, encoding="utf-8").read()
        self.assertNotIn("Quick question: would you like me to put together a quote", src)


class PromptGuidesToTheWebsiteNotToACall(Base):
    def prompt(self):
        self.turn("tell me more about bitumen 60/70", "It's a paving grade used for roads.")
        system = [m["content"] for m in self.sent_messages[-1] if m.get("role") == "system"]
        return " ".join("\n".join(system).split())     # the prompt is hard-wrapped: compare as one line

    def test_no_rule_tells_the_ai_to_pivot_to_a_quote_or_call_after_a_few_questions(self):
        p = self.prompt()
        self.assertNotIn("pivot toward a next step", p)
        self.assertNotIn("Freeform Q&A cap", p)

    def test_information_questions_get_the_page_link_and_no_call_offer(self):
        p = self.prompt().lower()
        self.assertIn("information questions", p)
        self.assertIn("read more", p)
        self.assertIn("do not offer a call", p)

    def test_the_offer_is_reserved_for_a_buyer_who_wants_to_proceed(self):
        p = self.prompt().lower()
        self.assertIn("only when the buyer", p)
        for signal in ("price", "quote", "book"):
            self.assertIn(signal, p[p.index("information questions"):p.index("information questions") + 1800])

    def test_a_detailed_buyer_is_not_pushed_to_booking_without_asking(self):
        p = self.prompt()
        self.assertNotIn("treat them as high-intent and move straight", p)


class CannedRepliesDoNotPushACall(Base):
    """The built-in replies (used when the knowledge base has no answer, or the AI model is unavailable) were also
    pushing a call + booking link on plain information questions."""

    def test_unanswerable_information_question_points_to_the_website_not_a_call(self):
        out = self.turn("what is the packaging for bitumen 60/70 spec?", "unused", refs=[], draft_only=True)
        self.assertNotIn("call", out.lower()); self.assertNotIn("cal.com", out)
        self.assertIn("https://www.petrobindglobal.com", out)
        self.assertIsNone(cs._SESSIONS["60177700009"].booking_link_shared_at)

    def test_a_price_question_still_gets_the_sales_director_line(self):
        out = self.turn("what is the price of bitumen 60/70?", "unused", refs=[], draft_only=True)
        self.assertIn("let me check with my sales director about the latest price", out)

    def test_when_the_ai_model_is_down_an_information_question_gets_no_call_offer(self):
        class Boom:
            chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=None))
        async def boom(**kw): raise RuntimeError("model unavailable")
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=boom)))
        out = self.turn("tell me more about bitumen 60/70", "unused", draft_only=True, client_override=client)
        self.assertNotIn("call", out.lower()); self.assertNotIn("cal.com", out); self.assertNotIn("sales director", out.lower())

    def test_when_the_ai_model_is_down_a_buyer_asking_for_a_call_still_gets_the_link(self):
        async def boom(**kw): raise RuntimeError("model unavailable")
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=boom)))
        out = self.turn("can we schedule a call to discuss?", "unused", draft_only=True, client_override=client)
        self.assertIn(self.cal, out)


class LinkFromKnowledgeBase(Base):
    def test_the_reference_block_gives_the_page_url_the_ai_may_link(self):
        out = L._format_references([CHUNK])
        self.assertIn("https://www.petrobindglobal.com/products/bitumen-60-70", out)


if __name__ == "__main__":
    unittest.main()
