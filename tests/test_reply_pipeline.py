"""Regression + behaviour tests for the reply pipeline (no network: OpenRouter is faked)."""
import asyncio
import os
import types
import unittest
from unittest import mock

os.environ.setdefault("OPENROUTER_API_KEY", "test")

import conversation_store as cs
import llm_assistant as L
import reply_guard as g


def _fake_client(contents):
    """Client whose chat.completions.create returns the given contents in order."""
    calls = {"n": 0, "messages": []}

    async def create(**kw):
        calls["messages"].append(kw["messages"])
        text = contents[min(calls["n"], len(contents) - 1)]
        calls["n"] += 1
        msg = types.SimpleNamespace(content=text, tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return client, calls


class SingleTurnSignature(unittest.TestCase):
    def test_accepts_draft_only_and_extra_system(self):
        """Regression: handle_incoming_message passes draft_only; a missing param silently forced every turn to the canned fallback."""
        client, calls = _fake_client(["Sure, which grade do you need?"])
        s = cs.ConversationSession(phone_number="60100000001")
        s.append("user", "hi")
        with mock.patch.object(L, "_openrouter_client", return_value=client):
            text, tools = asyncio.run(L._single_turn_chat(session=s, references=[], draft_only=True, extra_system="GUIDANCE-MARKER"))
        self.assertEqual(text, "Sure, which grade do you need?")
        sys_msgs = [m["content"] for m in calls["messages"][0] if m["role"] == "system"]
        self.assertTrue(any("GUIDANCE-MARKER" in c for c in sys_msgs))


class ReplyGuard(unittest.TestCase):
    def test_human_requests(self):
        for t in ("Can I speak to a real person?", "can someone call me", "I want a human", "talk to your sales director"):
            self.assertTrue(g.asks_for_human(t), t)
        for t in ("What is the price of VG30?", "send me the datasheet"):
            self.assertFalse(g.asks_for_human(t), t)

    def test_repeats_removed_but_not_empty(self):
        prev = ["Thanks for reaching out. We supply Bitumen 60/70 in new steel drums."]
        out = g.strip_repeats("Thanks for reaching out. We supply Bitumen 60/70 in new steel drums. Lead time is about two weeks.", prev)
        self.assertEqual(out, "Lead time is about two weeks.")
        only = "We supply Bitumen 60/70 in new steel drums."
        self.assertEqual(g.strip_repeats(only, prev), only)

    def test_links_survive(self):
        prev = ["You can book a call here https://cal.com/x/30min if helpful."]
        out = g.strip_repeats("You can book a call here https://cal.com/x/30min if helpful. Anything else?", prev)
        self.assertIn("https://cal.com/x/30min", out)


if __name__ == "__main__":
    unittest.main()
