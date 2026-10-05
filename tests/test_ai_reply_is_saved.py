"""RED first. Every message the AI sends to WhatsApp must also be saved to the inbox database, otherwise the inbox
(which reads that database) never shows the AI's reply."""
import asyncio
import os
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

REAL_SLEEP = asyncio.sleep


class FakeMeta:
    """Stands in for the WhatsApp Graph API."""
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, headers=None, **k):
        class R:
            status_code = 200
            content = b"{}"
            text = "{}"
            def json(self_inner): return {"messages": [{"id": "wamid.AI-" + str(abs(hash(json['text']['body'])) % 10_000)}]}
        return R()


class AiReplyIsSaved(unittest.TestCase):
    def _run_reply(self, reply):
        import main
        saved = []

        async def record(**kw):
            saved.append(kw)

        async def handle(**kw):
            return reply

        async def not_held(_n, _s=None):
            return None

        async def fast_sleep(_s):
            await REAL_SLEEP(0)

        async def go():
            await main._llm_reply(from_number="60134610868", phone_number_id="1", inbound_text="Hi, can I ask who are you?", reply_to_message_id=None)
            await REAL_SLEEP(0.05)                    # let the background saves run, as they do in the long-lived server

        with mock.patch.object(main._sb, "ENABLED", True), mock.patch.object(main._sb, "insert_outbound_message", record), \
                mock.patch.object(main.llm_assistant, "handle_incoming_message", handle), mock.patch.object(main, "_claim_is_held_by_other", not_held), \
                mock.patch.object(main.httpx, "AsyncClient", FakeMeta), mock.patch.object(main, "ACCESS_TOKEN", "t"), mock.patch.object(main, "PHONE_NUMBER_ID", "1"), \
                mock.patch.object(main.asyncio, "sleep", fast_sleep):
            asyncio.run(go())
        return saved

    def test_a_single_message_reply_is_saved(self):
        saved = self._run_reply("Hi, I'm Jane from Petrobind. How can I help?")
        self.assertEqual([(s["direction"], s["text"]) for s in saved], [("ai", "Hi, I'm Jane from Petrobind. How can I help?")])

    def test_every_part_of_a_multi_message_reply_is_saved(self):
        reply = "Hi, I'm Jane from Petrobind. Happy to help you with bitumen.\n\nWhat product and quantity are you looking at, and to which port should it go?"
        saved = self._run_reply(reply)
        self.assertEqual(len(saved), 2, saved)
        self.assertTrue(all(s["direction"] == "ai" and s["text"] for s in saved))
        self.assertTrue(all(s.get("sent_id_from_graph") for s in saved), "saved rows need the WhatsApp message id so delivery ticks can update them")


if __name__ == "__main__":
    unittest.main()


class SavingIsDependable(unittest.TestCase):
    """A sent message must be in the database by the time the send returns (no background task that can be lost),
    a hiccup is retried once, and a final failure is recorded where it can be read, never swallowed."""

    def _send(self, insert, audit):
        import main

        async def fast_sleep(_s):
            await REAL_SLEEP(0)

        async def go():
            return await main.send_whatsapp_text(to="60134610868", text="hello there", sender_direction="ai")   # NO extra waiting afterwards

        with mock.patch.object(main._sb, "ENABLED", True), mock.patch.object(main._sb, "insert_outbound_message", insert), \
                mock.patch.object(main._sb, "audit", audit), mock.patch.object(main.httpx, "AsyncClient", FakeMeta), \
                mock.patch.object(main, "ACCESS_TOKEN", "t"), mock.patch.object(main, "PHONE_NUMBER_ID", "1"), mock.patch.object(main.asyncio, "sleep", fast_sleep):
            return asyncio.run(go())

    def test_the_row_is_saved_before_the_send_returns(self):
        saved = []

        async def insert(**kw):
            saved.append(kw)

        self._send(insert, mock.AsyncMock())
        self.assertEqual([s["text"] for s in saved], ["hello there"])

    def test_one_hiccup_is_retried(self):
        calls = {"n": 0}
        saved = []

        async def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("connection reset")
            saved.append(kw)

        self._send(flaky, mock.AsyncMock())
        self.assertEqual((calls["n"], len(saved)), (2, 1))

    def test_a_final_failure_is_logged_for_the_admin_and_does_not_break_the_send(self):
        async def broken(**kw):
            raise RuntimeError("boom: database unreachable")

        audit = mock.AsyncMock()
        result = self._send(broken, audit)
        self.assertIn("messages", result)                                   # the WhatsApp send itself still succeeded
        actions = [c.kwargs.get("action") for c in audit.await_args_list]
        self.assertIn("persist_failed", actions)
        detail = next(c.kwargs["detail"] for c in audit.await_args_list if c.kwargs.get("action") == "persist_failed")
        self.assertIn("boom", detail["error"])
