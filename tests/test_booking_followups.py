"""RED first. Booking follow-up rules (the buyer was sent the Cal.com link):
  - never remind someone who booked or said no
  - one reminder 2h after the link; a second one only if the buyer answered with interest, 2h after their answer
  - never more than 2 reminders, never after 24h
  - afterwards the buyer is put in the human follow-up queue
and booking detection must be able to tell WHICH chat a Cal.com booking belongs to."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import booking
import conversation_store as cs
import reply_guard as g
import supabase_client as sb

H = 3600
T0 = 1_800_000_000.0


def _sess(**kw):
    s = cs.ConversationSession(phone_number="60123456789")
    s.booking_link_shared_at = T0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _act(s, hours):
    return cs.booking_followup_action(s, T0 + hours * H)


class BookingFollowupRules(unittest.TestCase):
    def test_nothing_before_two_hours(self):
        self.assertIsNone(_act(_sess(), 1.9))

    def test_first_reminder_at_two_hours(self):
        a = _act(_sess(), 2.0)
        self.assertEqual(a.kind, "booking_reminder")
        self.assertIn(booking.CAL_COM_BOOKING_LINK or "", a.message_text)

    def test_booked_or_declined_buyers_are_left_alone(self):
        self.assertIsNone(_act(_sess(booking_confirmed_at=T0 + H), 3))
        self.assertIsNone(_act(_sess(booking_intent="declined"), 3))
        self.assertIsNone(_act(_sess(booking_reminders_sent=1, booking_last_reminder_at=T0 + 2 * H, booking_intent="declined"), 9))

    def test_silent_buyer_gets_one_reminder_then_a_human(self):
        s = _sess(booking_reminders_sent=1, booking_last_reminder_at=T0 + 2 * H)
        self.assertIsNone(_act(s, 3.9))                       # still waiting for an answer
        self.assertEqual(_act(s, 4.0).kind, "booking_human_flag")

    def test_interested_buyer_gets_a_second_reminder_two_hours_after_answering(self):
        s = _sess(booking_reminders_sent=1, booking_last_reminder_at=T0 + 2 * H, booking_intent="interested", booking_intent_at=T0 + 3 * H)
        self.assertIsNone(_act(s, 4.9))
        a = _act(s, 5.0)
        self.assertEqual(a.kind, "booking_reminder")

    def test_second_reminder_wording_differs_from_the_first(self):
        s1 = _sess(); first = _act(s1, 2).message_text
        s2 = _sess(booking_reminders_sent=1, booking_last_reminder_at=T0 + 2 * H, booking_intent="later", booking_intent_at=T0 + 3 * H)
        self.assertNotEqual(first, _act(s2, 5).message_text)

    def test_after_two_reminders_a_human_takes_over(self):
        s = _sess(booking_reminders_sent=2, booking_last_reminder_at=T0 + 5 * H, booking_intent="interested", booking_intent_at=T0 + 3 * H)
        self.assertIsNone(_act(s, 6.9))
        self.assertEqual(_act(s, 7.0).kind, "booking_human_flag")

    def test_nothing_automatic_after_24_hours_except_a_single_human_flag(self):
        self.assertEqual(_act(_sess(), 25).kind, "booking_human_flag")          # never reminded in time -> straight to a human
        flagged = _sess(booking_human_flagged_at=T0 + 25 * H)
        self.assertIsNone(_act(flagged, 30))

    def test_reminder_limit_can_be_lowered_to_strictly_once(self):
        s = _sess(booking_reminders_sent=1, booking_last_reminder_at=T0 + 2 * H, booking_intent="interested", booking_intent_at=T0 + 3 * H)
        with mock.patch.object(cs, "BOOKING_MAX_REMINDERS", 1):
            self.assertEqual(_act(s, 5).kind, "booking_human_flag")

    def test_scan_returns_the_booking_action(self):
        cs._SESSIONS.clear(); cs._SESSIONS["60123456789"] = _sess()
        with mock.patch.object(cs, "_now", lambda: T0 + 2.5 * H):
            kinds = [a.kind for a in cs.scan_for_followups()]
        cs._SESSIONS.clear()
        self.assertEqual(kinds, ["booking_reminder"])

    def test_marking_sent_counts_reminders_and_flags_humans(self):
        cs._REDIS_URL = cs._REDIS_TOKEN = ""
        s = _sess(); cs._SESSIONS["60123456789"] = s
        asyncio.run(cs.mark_followup_sent("60123456789", "booking_reminder"))
        self.assertEqual((s.booking_reminders_sent, bool(s.booking_last_reminder_at)), (1, True))
        asyncio.run(cs.mark_followup_sent("60123456789", "booking_human_flag"))
        self.assertTrue(s.needs_human_since); self.assertIn("booking", s.needs_human_reason.lower()); self.assertTrue(s.booking_human_flagged_at)
        cs._SESSIONS.clear()


class BookingIntent(unittest.TestCase):
    def test_classifies_replies(self):
        cases = {"yes sure, will book tonight": "interested", "ok send me the link again": "interested", "what times are open?": "interested",
                 "maybe next week, I'm busy now": "later", "let me check with my boss first": "later",
                 "not interested, thanks": "declined", "we already have a supplier": "declined", "please stop messaging me": "declined",
                 "hmm": "unclear", "what is the price of VG30?": "unclear"}
        for text, want in cases.items():
            self.assertEqual(g.classify_booking_intent(text), want, text)


class BookingDetection(unittest.TestCase):
    def setUp(self):
        cs._SESSIONS.clear()
        s = cs.ConversationSession(phone_number="60123456789"); s.inquiry.contact_email = "buyer@acme.com"
        cs._SESSIONS["60123456789"] = s
        self.s = s

    def tearDown(self):
        cs._SESSIONS.clear()

    def test_matches_by_whatsapp_metadata(self):
        self.assertIs(booking.find_session_for_booking({"metadata": {"whatsapp": "60123456789"}}), self.s)

    def test_matches_by_attendee_phone_even_with_plus_and_spaces(self):
        p = {"attendees": [{"name": "A", "email": "x@y.com", "phoneNumber": "+60 12-345 6789"}]}
        self.assertIs(booking.find_session_for_booking(p), self.s)
        self.assertIs(booking.find_session_for_booking({"responses": {"attendeePhoneNumber": {"value": "+60123456789"}}}), self.s)

    def test_matches_by_attendee_email_captured_in_chat(self):
        self.assertIs(booking.find_session_for_booking({"attendees": [{"email": "Buyer@Acme.com"}]}), self.s)

    def test_unknown_booking_matches_nothing(self):
        self.assertIsNone(booking.find_session_for_booking({"attendees": [{"email": "stranger@else.com", "phoneNumber": "+6599999999"}]}))

    def test_shared_link_identifies_the_chat(self):
        with mock.patch.object(booking, "CAL_COM_BOOKING_LINK", "https://cal.com/petrobindglobal/30min"):
            url = booking.get_booking_link(phone="60123456789", name="Ahmad", email="buyer@acme.com")
        self.assertTrue(url.startswith("https://cal.com/petrobindglobal/30min?"))
        for needle in ("whatsapp%5D=60123456789", "attendeePhoneNumber=%2B60123456789", "email=buyer%40acme.com", "name=Ahmad"):
            self.assertIn(needle, url)
        with mock.patch.object(booking, "CAL_COM_BOOKING_LINK", "https://cal.com/x/30min"):
            self.assertEqual(booking.get_booking_link(), "https://cal.com/x/30min")   # unchanged without a phone

    def test_webhook_stamps_the_session_and_logs_a_match(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tmp.close(); os.remove(tmp.name)
        sb._AUDIT_FILE = tmp.name
        cs._REDIS_URL = cs._REDIS_TOKEN = ""
        with mock.patch.object(booking.notify, "send_booking_email", mock.AsyncMock(return_value=True)), mock.patch.object(sb, "ENABLED", False):
            body, code = asyncio.run(booking.handle_cal_webhook({"triggerEvent": "BOOKING_CREATED", "attendees": [{"name": "A", "email": "buyer@acme.com"}]}))
            events = asyncio.run(sb.list_audit(limit=5))
        self.assertEqual(code, 200)
        self.assertTrue(self.s.booking_confirmed_at)
        self.assertEqual(events[0]["action"], "booking_confirmed")
        os.remove(tmp.name)

    def test_unmatched_booking_is_logged_not_lost(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False); tmp.close(); os.remove(tmp.name)
        sb._AUDIT_FILE = tmp.name
        with mock.patch.object(booking.notify, "send_booking_email", mock.AsyncMock(return_value=True)), mock.patch.object(sb, "ENABLED", False):
            asyncio.run(booking.handle_cal_webhook({"triggerEvent": "BOOKING_CREATED", "attendees": [{"name": "Z", "email": "stranger@else.com"}]}))
            events = asyncio.run(sb.list_audit(limit=5))
        self.assertEqual(events[0]["action"], "booking_unmatched")
        self.assertFalse(self.s.booking_confirmed_at)
        os.remove(tmp.name)


if __name__ == "__main__":
    unittest.main()
