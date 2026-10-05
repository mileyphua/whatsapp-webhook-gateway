import datetime
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestScheduler(unittest.TestCase):

    def test_buyer_local_hour_utc_offset_sg(self):
        from scheduler import buyer_local_hour_utc_offset
        result = buyer_local_hour_utc_offset("+6512345678")
        self.assertIsNotNone(result)
        self.assertTrue(abs(result - 8.0) < 0.001)

    def test_quiet_window_sg_midnight(self):
        from scheduler import compute_next_sendable_utc_timestamp
        now_utc = datetime.datetime(2026, 10, 6, 20, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        ok, tgt = compute_next_sendable_utc_timestamp(now_utc, 8.0)
        self.assertFalse(ok)
        tgt_dt = datetime.datetime.fromtimestamp(tgt, datetime.timezone.utc)
        self.assertEqual(tgt_dt.hour, 23)
        sg_local = datetime.datetime.fromtimestamp(tgt + 8.0 * 3600, datetime.timezone.utc)
        sg_weekday = sg_local.weekday()
        self.assertIn(sg_weekday, (0, 1, 2, 3, 4))

    def test_okay_window_sg_1500(self):
        from scheduler import compute_next_sendable_utc_timestamp
        now_utc = datetime.datetime(2026, 10, 6, 7, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        ok, tgt = compute_next_sendable_utc_timestamp(now_utc, 8.0)
        self.assertTrue(ok)

    def test_unknown_tz_prefix_fail_open(self):
        from scheduler import compute_next_sendable_utc_timestamp
        ok, tgt = compute_next_sendable_utc_timestamp(time.time(), None, "+886999")
        self.assertTrue(ok)

    def test_country_prefix_matcher(self):
        from scheduler import _country_prefix_from_e164
        self.assertEqual(_country_prefix_from_e164("+12045551234"), "1204")
        self.assertEqual(_country_prefix_from_e164("+6591234567"), "65")
        self.assertEqual(_country_prefix_from_e164("+14155559876"), "1")


if __name__ == "__main__":
    unittest.main()
