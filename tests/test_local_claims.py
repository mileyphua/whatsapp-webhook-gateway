"""RED first. Without Supabase (local dev) chat locks must still work in-process, so two people can see each other's
lock; and the thread must notice a lock (or its release) within seconds, not half a minute."""
import asyncio
import os
import re
import unittest
from unittest import mock

import supabase_client as sb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E = "60123456789"


class LocalClaims(unittest.TestCase):
    def setUp(self):
        self._p = mock.patch.object(sb, "ENABLED", False); self._p.start()
        sb._LOCAL_CLAIMS.clear()

    def tearDown(self):
        self._p.stop(); sb._LOCAL_CLAIMS.clear()

    def run_(self, c):
        return asyncio.run(c)

    def test_a_lock_is_visible_to_everyone_with_the_holders_name(self):
        ok, conflict = self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))
        self.assertEqual((ok, conflict), (True, None))
        held = self.run_(sb.claim_is_human_held(E))
        self.assertEqual((held["held_by"], held["session_id"]), ("Mei Ling", "u:mei"))
        self.assertGreater(held["expires_in_secs"], 0)

    def test_someone_else_cannot_take_a_held_chat_but_the_holder_can_refresh(self):
        self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))
        ok, conflict = self.run_(sb.claim_acquire(e164=E, held_by="Petrobind Admin", session_id="u:admin", ttl_seconds=600))
        self.assertFalse(ok); self.assertEqual(conflict["held_by"], "Mei Ling")
        self.assertTrue(self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))[0])

    def test_only_the_holder_releases_and_the_admin_can_force(self):
        self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))
        self.run_(sb.claim_release(e164=E, held_by=None, session_id="u:raj"))
        self.assertIsNotNone(self.run_(sb.claim_is_human_held(E)))
        self.run_(sb.claim_release(e164=E, held_by=None, session_id="u:mei"))
        self.assertIsNone(self.run_(sb.claim_is_human_held(E)))
        self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))
        self.run_(sb.claim_force_release(E))
        self.assertIsNone(self.run_(sb.claim_is_human_held(E)))

    def test_a_lock_expires(self):
        self.run_(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=60))
        with mock.patch.object(sb.time, "time", return_value=__import__("time").time() + 120):
            self.assertIsNone(self.run_(sb.claim_is_human_held(E)))
            self.assertTrue(self.run_(sb.claim_acquire(e164=E, held_by="Raj", session_id="u:raj", ttl_seconds=60))[0])


class ThreadNoticesLocksQuickly(unittest.TestCase):
    def setUp(self):
        self.js = open(os.path.join(ROOT, "petrobind_frontend_app", "static", "app.js"), encoding="utf-8").read()

    def test_lock_status_is_checked_every_few_seconds(self):
        m = re.search(r"setInterval\((?:async )?function \(\) \{[\s\S]*?loadMode\(\)[\s\S]*?\},\s*(\d+)\)", self.js)
        self.assertIsNotNone(m, "no periodic loadMode() poll found")
        self.assertLessEqual(int(m.group(1)), 5000)

    def test_a_released_lock_is_not_silently_taken_back(self):
        """The 30s renewal used to POST an acquire even after the admin force-released, quietly re-taking the chat."""
        self.assertRegex(self.js, r"if \(humanMode[^;]*goHuman\(\)")                    # renewal only while still holding it
        self.assertNotRegex(self.js, r"setInterval\(function \(\) \{ if \(humanMode\) goHuman\(\); else loadMode\(\); \}, 30 \* 1000\)")


if __name__ == "__main__":
    unittest.main()


class ListShowsLocalLocks(unittest.TestCase):
    def test_chat_list_marks_the_chat_with_the_holders_name(self):
        import conversation_store as cs
        import main
        s = cs.ConversationSession(phone_number=E); s.append("user", "hi"); s.append("assistant", "hello")
        cs._SESSIONS[E] = s
        try:
            with mock.patch.object(sb, "ENABLED", False), mock.patch.object(cs, "redis_scan_all_sessions", mock.AsyncMock(return_value=0)):
                sb._LOCAL_CLAIMS.clear()
                asyncio.run(sb.claim_acquire(e164=E, held_by="Mei Ling", session_id="u:mei", ttl_seconds=600))
                rows = asyncio.run(main._history_chats())
            self.assertEqual([r["claim_held_by"] for r in rows if r["e164"] == E], ["Mei Ling"])
        finally:
            cs._SESSIONS.pop(E, None); sb._LOCAL_CLAIMS.clear()
