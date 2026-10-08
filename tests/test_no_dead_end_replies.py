"""After a hand-off or a captured order the AI must not stop at 'a colleague will follow up':
it says what it understood and asks the ONE next thing that is still missing."""
import asyncio
import unittest
from unittest import mock

import conversation_store as cs
import llm_assistant as L


def run_tool(name, args, session=None):
    session = session or cs.ConversationSession(phone_number="60177700010")
    with mock.patch.object(L.notify, "send_lead_email", mock.AsyncMock(return_value=True)), \
            mock.patch.object(L.notify, "send_handoff_email", mock.AsyncMock(return_value=True)):
        return asyncio.run(L._run_tool(name, args, session=session, draft_only=False))


class CapturedOrder(unittest.TestCase):
    def test_tells_the_ai_to_confirm_and_ask_for_the_missing_contact(self):
        out = run_tool("capture_trade_inquiry", {"product": "CSS-1h", "quantity": "200 tonnes"})
        self.assertIn("ask for their company name", out)
        self.assertIn("ONE", out)
        self.assertNotIn("let them know a human will follow up directly", out)

    def test_nothing_missing_means_just_confirm(self):
        s = cs.ConversationSession(phone_number="60177700011")
        out = run_tool("capture_trade_inquiry", {
            "company_name": "Acme", "contact_email": "a@acme.com", "product": "R85/40", "quantity": "300 MT",
            "destination_port": "Jebel Ali", "incoterm": "CIF", "packaging": "drums"}, s)
        self.assertIn("confirm", out.lower())
        self.assertNotIn("ask for their company name", out)


class OtherHandoff(unittest.TestCase):
    def test_non_pricing_handoff_asks_one_useful_question(self):
        out = run_tool("request_sales_handoff", {"reason": "shipping times", "is_pricing": False})
        self.assertIn("ONE short question", out)

    def test_pricing_line_is_still_exact_and_alone(self):
        out = run_tool("request_sales_handoff", {"reason": "price", "is_pricing": True})
        self.assertIn("Hold on, let me check with my sales director about the latest price to confirm.", out)
        self.assertNotIn("ONE short question", out)


class Prompt(unittest.TestCase):
    def test_prompt_forbids_a_bare_colleague_will_follow_up(self):
        self.assertIn("Never end a reply on only", L.COMPANY_PROFILE)

    def test_comparisons_and_order_details_do_not_trigger_a_handoff(self):
        self.assertIn("do NOT call request_sales_handoff for it", L.COMPANY_PROFILE)
        self.assertIn("never also\n    request_sales_handoff", L.COMPANY_PROFILE)
        handoff = next(t for t in L.TOOL_DEFINITIONS if t["function"]["name"] == "request_sales_handoff")
        self.assertIn("NOT for comparing grades", handoff["function"]["description"])


class ComparingTwoGrades(unittest.TestCase):
    def test_names_each_grade_once(self):
        self.assertEqual(L._grades_named("difference between 60/70 and 80/100?"), ["60/70", "80/100"])
        self.assertEqual(L._grades_named("vg30 vs VG-40 and 60/70 again 60/70"), ["VG30", "VG40", "60/70"])
        self.assertEqual(L._grades_named("what is bitumen"), [])

    def test_each_grade_gets_its_own_search_and_results_are_merged_without_repeats(self):
        from rag.retrieve import RetrievedChunk
        def chunk(u, t): return RetrievedChunk(url=u, title=t, chunk_text=t, similarity=0.5, chunk_index=0)
        calls = []
        async def fake(q, **kw):
            calls.append(q)
            return {"Bitumen 60/70": [chunk("u1", "sixty")], "Bitumen 80/100": [chunk("u2", "eighty"), chunk("u1", "sixty")]}.get(q, [])
        with mock.patch.object(L, "retrieve", fake):
            out = asyncio.run(L._retrieve_for_grades("difference between 60/70 and 80/100?", [chunk("u2", "eighty")], None))
        self.assertEqual(calls, ["Bitumen 60/70", "Bitumen 80/100"])
        self.assertEqual(sorted(c.url for c in out), ["u1", "u2"])

    def test_one_grade_or_none_leaves_the_results_alone(self):
        first = [object()]
        self.assertIs(asyncio.run(L._retrieve_for_grades("tell me about 60/70", first, None)), first)
