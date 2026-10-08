"""Lead summary (RED first): a plain-language record of what a buyer wants and where the chat stands.

Built from the chat itself with fixed rules (no AI call), so it is instant, free and cannot invent facts.
"""
import os
import tempfile
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import lead_summary as ls


def B(text, **kw):
    return {"direction": "buyer", "text": text, "created_at": kw.pop("at", "2026-10-07T02:00:00+00:00"), **kw}


def A(text, **kw):
    return {"direction": "ai", "text": text, "created_at": kw.pop("at", "2026-10-07T02:01:00+00:00"), **kw}


def H(text, **kw):
    return {"direction": "human", "text": text, "created_at": kw.pop("at", "2026-10-07T02:05:00+00:00"), **kw}


class FirstContact(unittest.TestCase):
    def test_a_plain_hello_is_a_new_lead_with_nothing_known(self):
        s = ls.build([B("hi"), A("Hi 👋 this is Jane from Petrobind Global. How can I help you today?")])
        self.assertEqual(s["relationship_stage"], "new_lead")
        self.assertEqual(s["customer_intent"], "greeting_only")
        self.assertEqual(s["product_interest"], "unknown")
        self.assertEqual(s["quantity"], "unknown")
        self.assertEqual(s["destination"], "unknown")
        self.assertFalse(s["price_requested"])
        self.assertFalse(s["technical_docs_sent"])
        self.assertEqual(s["next_best_action"], "ask_requirement")

    def test_empty_chat_does_not_crash(self):
        s = ls.build([])
        self.assertEqual(s["relationship_stage"], "new_lead")
        self.assertEqual(s["topics"], [])


class WhatTheBuyerWants(unittest.TestCase):
    def test_an_information_question_is_product_information(self):
        s = ls.build([B("what are the specs of bitumen 60/70?"), A("60/70 has penetration 60-70 dmm...")])
        self.assertEqual(s["customer_intent"], "product_information")
        self.assertEqual(s["product_interest"], "Bitumen 60/70")
        self.assertFalse(s["price_requested"])

    def test_a_price_and_order_message_is_read_completely(self):
        s = ls.build([B("I want to buy 500 tonnes of 60/70 CFR Port Klang, what price?"),
                      A("Hold on, let me check with my sales director about the latest price to confirm.")])
        self.assertEqual(s["customer_intent"], "bitumen_purchase")
        self.assertEqual(s["product_interest"], "Bitumen 60/70")
        self.assertEqual(s["quantity"], "500 tonnes")
        self.assertEqual(s["destination"], "Port Klang")
        self.assertEqual(s["incoterm"], "CFR")
        self.assertTrue(s["price_requested"])
        self.assertEqual(s["next_best_action"], "confirm_price_with_sales")

    def test_grades_are_recognised(self):
        for text, label in [("specs for VG30?", "VG30"), ("specs of oxidized bitumen R85/40", "Oxidized bitumen R85/40"),
                            ("what are the specifications of emulsion CSS-1h?", "Bitumen emulsion CSS-1h")]:
            self.assertEqual(ls.build([B(text)])["product_interest"], label, text)

    def test_the_latest_product_wins(self):
        s = ls.build([B("tell me about 60/70"), A("..."), B("actually I need VG30 now")])
        self.assertEqual(s["product_interest"], "VG30")

    def test_a_call_request_is_a_call_request(self):
        self.assertEqual(ls.build([B("can we schedule a call tomorrow?")])["customer_intent"], "call_request")

    def test_a_certificate_request_is_a_documents_request(self):
        s = ls.build([B("can you send me a COA for 60/70?")])
        self.assertEqual(s["customer_intent"], "documents_request")

    def test_saved_lead_details_beat_guesses_from_the_text(self):
        s = ls.build([B("we need some bitumen")], inquiry={"product": "Oxidized Bitumen R85/40", "quantity": "300 MT",
                                                              "destination_port": "Jebel Ali", "company_name": "Acme Roads",
                                                              "contact_name": "Ali", "contact_email": "ali@acme.com"})
        self.assertEqual(s["product_interest"], "Oxidized Bitumen R85/40")
        self.assertEqual(s["quantity"], "300 MT")
        self.assertEqual(s["destination"], "Jebel Ali")
        self.assertEqual(s["company"], "Acme Roads")
        self.assertIn("Ali", s["contact"])
        self.assertIn("ali@acme.com", s["contact"])


class StageAndNextStep(unittest.TestCase):
    def test_product_known_but_no_quantity_asks_for_quantity(self):
        s = ls.build([B("I want to buy 60/70"), A("Sure. How many tonnes do you need?")])
        self.assertEqual(s["next_best_action"], "ask_quantity")

    def test_quantity_known_but_no_destination_asks_for_destination(self):
        s = ls.build([B("I need 200 tonnes of 60/70 for a road job")])
        self.assertEqual(s["next_best_action"], "ask_destination")

    def test_everything_known_and_no_price_asked_prepares_the_quote(self):
        s = ls.build([B("I need 200 tonnes of 60/70 to Jebel Ali FOB")],
                     inquiry={"company_name": "Acme", "contact_name": "Ali"})
        self.assertEqual(s["relationship_stage"], "qualified_lead")
        self.assertEqual(s["next_best_action"], "prepare_quote")

    def test_wanting_to_proceed_is_ready_to_order(self):
        s = ls.build([B("I want to proceed with the order, 200 tonnes of emulsion CSS-1h to Jebel Ali, FOB, drums")])
        self.assertEqual(s["relationship_stage"], "ready_to_order")
        self.assertEqual(s["packaging"], "drums")

    def test_a_chat_that_has_been_handed_over_says_a_person_must_take_over(self):
        s = ls.build([B("hello"), A("...")], state={"needs_human_since": 1.0, "needs_human_reason": "price question"})
        self.assertEqual(s["next_best_action"], "human_take_over")
        self.assertTrue(any("price question" in x for x in s["needs_human_confirmation"]))

    def test_a_paused_ai_is_called_out(self):
        s = ls.build([B("voice note")], state={"ai_paused": True, "ai_paused_reason": "voice note"})
        self.assertEqual(s["next_best_action"], "human_take_over")
        self.assertTrue(any("voice note" in x for x in s["needs_human_confirmation"]))

    def test_a_booked_call_is_stage_call_booked(self):
        s = ls.build([B("can we schedule a call?")], state={"booking_confirmed": True})
        self.assertEqual(s["relationship_stage"], "call_booked")

    def test_a_chat_with_a_few_questions_is_exploring(self):
        s = ls.build([B("what is bitumen emulsion used for?"), A("..."), B("tell me more about 60/70"), A("..."),
                      B("what packaging do you offer?"), A("...")])
        self.assertEqual(s["relationship_stage"], "exploring")


class DocumentsAndTone(unittest.TestCase):
    def test_a_document_we_sent_counts_as_technical_docs_sent(self):
        s = ls.build([B("send COA"), H("Here you go", media_type="document", filename="COA-60-70.pdf")])
        self.assertTrue(s["technical_docs_sent"])

    def test_promising_a_document_is_not_sending_it(self):
        s = ls.build([B("send COA"), A("I'll get the COA ready for you.")])
        self.assertFalse(s["technical_docs_sent"])

    def test_short_messages_are_short_business_tone(self):
        self.assertEqual(ls.build([B("price 60/70?"), B("500 mt CFR")])["tone"], "short_business")

    def test_long_messages_are_detailed_tone(self):
        long = "Good afternoon, we are a road construction company planning a large project next quarter and would like " \
               "to understand the available grades, the packaging options, the typical lead time for delivery and the " \
               "payment terms that you normally offer to new customers in our region."
        self.assertEqual(ls.build([B(long)])["tone"], "detailed")


class HumanConfirmation(unittest.TestCase):
    def test_price_always_needs_the_sales_director(self):
        s = ls.build([B("what is the price of 60/70?"), A("Hold on, let me check with my sales director.")])
        self.assertTrue(any("price" in x.lower() for x in s["needs_human_confirmation"]))

    def test_a_certification_question_is_listed(self):
        s = ls.build([B("do you have any certifications?"), A("I'll have a colleague confirm which certifications apply.")])
        self.assertTrue(any("certif" in x.lower() for x in s["needs_human_confirmation"]))

    def test_a_buyer_message_without_a_reply_is_listed(self):
        s = ls.build([B("hello?")])
        self.assertTrue(any("no reply" in x.lower() for x in s["needs_human_confirmation"]))

    def test_a_failed_send_is_listed(self):
        s = ls.build([B("hi"), A("Hello", errored=True, error_detail="131047 re-engagement")])
        self.assertTrue(any("failed" in x.lower() for x in s["needs_human_confirmation"]))

    def test_nothing_to_confirm_gives_an_empty_list(self):
        s = ls.build([B("what is bitumen emulsion used for?"), A("It is used for tack coats and surface dressing.")])
        self.assertEqual(s["needs_human_confirmation"], [])

    def test_no_duplicates(self):
        s = ls.build([B("price?"), A("..."), B("price for 60/70?"), A("...")])
        self.assertEqual(len(s["needs_human_confirmation"]), len(set(s["needs_human_confirmation"])))


class ConversationRecord(unittest.TestCase):
    def test_topics_list_what_was_talked_about_in_order(self):
        s = ls.build([B("hi"), A("Hello"), B("what are the specs of 60/70?"), A("Penetration 60-70 dmm..."),
                      B("what packaging do you offer?"), A("Drums, bulk and jumbo bags.")])
        self.assertEqual([t["topic"] for t in s["topics"]], ["Greeting", "Specifications", "Packaging"])
        self.assertEqual(s["topics"][1]["buyer"], "what are the specs of 60/70?")
        self.assertIn("Penetration", s["topics"][1]["reply"])
        self.assertEqual(s["topics"][1]["by"], "ai")

    def test_an_unanswered_question_has_no_reply(self):
        s = ls.build([B("what is the price of 60/70?")])
        self.assertEqual(s["topics"][0]["reply"], "")
        self.assertIsNone(s["topics"][0]["by"])

    def test_a_human_answer_is_marked_as_human(self):
        s = ls.build([B("price for 60/70?"), H("USD 480 per tonne CFR", held_by="Mei Ling")])
        self.assertEqual(s["topics"][0]["by"], "human")

    def test_remark_names_the_last_message_and_the_conclusion(self):
        s = ls.build([B("I want to buy 500 tonnes of 60/70 CFR Port Klang, what price?"),
                      A("Hold on, let me check with my sales director about the latest price to confirm.")])
        self.assertIn("500 tonnes of 60/70", s["remark"])
        self.assertIn("sales director", s["remark"].lower())

    def test_long_texts_are_shortened(self):
        s = ls.build([B("x" * 900), A("y" * 900)])
        self.assertLessEqual(len(s["topics"][0]["buyer"]), 200)
        self.assertLessEqual(len(s["topics"][0]["reply"]), 200)

    def test_only_the_recent_topics_are_kept(self):
        msgs = []
        for i in range(30):
            msgs += [B(f"what are the specs of {60 + i}/70?"), A("ok")]
        self.assertLessEqual(len(ls.build(msgs)["topics"]), 12)

    def test_counts_and_last_buyer_time(self):
        s = ls.build([B("hi", at="2026-10-07T02:00:00+00:00"), A("hello"), B("price?", at="2026-10-07T03:00:00+00:00")])
        self.assertEqual(s["buyer_messages"], 2)
        self.assertEqual(s["last_buyer_at"], "2026-10-07T03:00:00+00:00")


class Endpoint(unittest.TestCase):
    def setUp(self):
        import users_store as us
        from fastapi.testclient import TestClient
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import asyncio
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(main._refresh_users_cache())
        self.agent = TestClient(main.app)
        self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        self.admin = TestClient(main.app)
        self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.anon = TestClient(main.app)

    def tearDown(self):
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def test_anonymous_cannot_read_a_summary(self):
        self.assertEqual(self.anon.get("/api/inbox/chats/60123456789/summary").status_code, 401)

    def test_admin_and_member_both_get_a_summary(self):
        for c in (self.admin, self.agent):
            r = c.get("/api/inbox/chats/60123456789/summary")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["e164"], "60123456789")
            for key in ("relationship_stage", "customer_intent", "product_interest", "quantity", "destination",
                        "price_requested", "technical_docs_sent", "next_best_action", "tone", "remark",
                        "needs_human_confirmation", "topics"):
                self.assertIn(key, body["summary"], key)

    def test_the_summary_reads_the_real_chat(self):
        msgs = [B("I want to buy 500 tonnes of 60/70 CFR Port Klang, what price?")]
        from unittest import mock
        with mock.patch.object(self.main, "_history_thread", mock.AsyncMock(return_value=msgs)):
            body = self.admin.get("/api/inbox/chats/60123456789/summary").json()
        self.assertEqual(body["summary"]["quantity"], "500 tonnes")
        self.assertTrue(body["summary"]["price_requested"])

    def test_it_reads_nothing_it_should_not_create_a_session(self):
        import conversation_store as cs
        before = set(cs._SESSIONS)
        self.admin.get("/api/inbox/chats/60999000111/summary")
        self.assertEqual(set(cs._SESSIONS), before)


class Page(unittest.TestCase):
    def setUp(self):
        import asyncio
        from fastapi.testclient import TestClient
        import main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def test_the_thread_page_has_a_summary_panel_that_can_be_shown_and_hidden(self):
        html = self.c.get("/inbox/chat/60123456789").text
        self.assertIn('id="lead-panel"', html)
        self.assertIn('id="lead-toggle"', html)
        self.assertIn('id="lead-body"', html)


if __name__ == "__main__":
    unittest.main()
