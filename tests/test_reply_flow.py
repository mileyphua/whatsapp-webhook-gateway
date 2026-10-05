"""RED first. How the AI sends: no quoting of the buyer's message, humanised split into a few messages, and the
admin can quote-reply to a specific message (like WhatsApp)."""
import asyncio
import os
import re
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import reply_guard as g

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Humanise(unittest.TestCase):
    def test_short_reply_is_one_message(self):
        self.assertEqual(g.humanize_parts("Sure, 20 tonnes works. Which port?"), ["Sure, 20 tonnes works. Which port?"])

    def test_long_multi_paragraph_reply_is_split_into_at_most_three_messages(self):
        text = "Yes, we supply Bitumen 60/70 in new steel drums to Port Klang, and flexibitanks are possible too.\n\nLead time is usually about two weeks from confirmation, depending on the loading schedule.\n\nWhich quantity are you looking at?\n\nAnd roughly when do you need it?"
        parts = g.humanize_parts(text)
        self.assertTrue(2 <= len(parts) <= 3, parts)
        self.assertEqual(" ".join(p.replace("\n\n", " ") for p in parts).split(), text.replace("\n\n", " ").split())  # nothing lost or reordered

    def test_a_link_stays_with_its_sentence(self):
        text = "Happy to set up a call, it only takes thirty minutes and you pick the time that suits you best.\n\nHere is the booking link: https://cal.com/x/30min\n\nTalk soon!"
        for p in g.humanize_parts(text):
            self.assertFalse(p.strip().endswith(":"), "a message must not end on a dangling colon before its link")


class AiDoesNotQuoteTheBuyer(unittest.TestCase):
    def test_llm_reply_sends_without_a_reply_context(self):
        import main
        sent = []

        async def fake_send(**kw):
            sent.append(kw); return {"messages": [{"id": "wamid.X%d" % len(sent)}]}

        async def fake_handle(**kw):
            return "Yes, we do supply that.\n\nHow many tonnes are you looking at? And to which port should it go, so I can check the lead time properly?"

        async def not_held(_n):
            return None

        async def no_sleep(_s):
            return None

        with mock.patch.object(main, "send_whatsapp_text", fake_send), mock.patch.object(main.llm_assistant, "handle_incoming_message", fake_handle), \
                mock.patch.object(main, "_claim_is_held_by_other", not_held), mock.patch.object(main.asyncio, "sleep", no_sleep):
            asyncio.run(main._llm_reply(from_number="60123456789", phone_number_id="1", inbound_text="hey", reply_to_message_id="wamid.BUYER"))
        self.assertTrue(sent, "nothing was sent")
        self.assertTrue(all(not k.get("reply_to_message_id") for k in sent), "the AI must not quote the buyer's message")
        self.assertGreaterEqual(len(sent), 2)             # humanised into a couple of messages

    def test_human_taking_over_mid_reply_stops_the_remaining_parts(self):
        import main
        sent = []
        calls = {"n": 0}

        async def fake_send(**kw):
            sent.append(kw["text"]); return {"messages": [{"id": "w"}]}

        async def fake_handle(**kw):
            return "First part is here, with enough words to count as a real message on its own.\n\nSecond part follows with more detail about shipping times and so on.\n\nThird part."

        async def held_after_first(_n):
            calls["n"] += 1
            return None if calls["n"] == 1 else {"held_by": "Admin", "expires_in_secs": 100}

        async def no_sleep(_s):
            return None

        with mock.patch.object(main, "send_whatsapp_text", fake_send), mock.patch.object(main.llm_assistant, "handle_incoming_message", fake_handle), \
                mock.patch.object(main, "_claim_is_held_by_other", held_after_first), mock.patch.object(main.asyncio, "sleep", no_sleep):
            asyncio.run(main._llm_reply(from_number="60123456789", phone_number_id="1", inbound_text="hey", reply_to_message_id=None))
        self.assertEqual(len(sent), 1)


class AdminQuoteReply(unittest.TestCase):
    def _js(self):
        return open(os.path.join(ROOT, "petrobind_frontend_app", "static", "app.js"), encoding="utf-8").read()

    def test_composer_has_a_quote_preview(self):
        html = open(os.path.join(ROOT, "petrobind_frontend_app", "templates", "chat_thread.html"), encoding="utf-8").read()
        self.assertIn('id="quote-preview"', html)

    def test_send_quotes_only_what_the_admin_picked(self):
        js = self._js()
        self.assertNotIn("reply_to_wamid: (ctx.last_buyer_wamid || null)", js, "must not auto-quote the last buyer message")
        self.assertRegex(js, r"reply_to_wamid:\s*quoted\s*\?\s*quoted\.wamid\s*:\s*null")

    def test_buyer_and_ai_bubbles_get_a_reply_button(self):
        self.assertIn("decorateReply", self._js())


if __name__ == "__main__":
    unittest.main()
