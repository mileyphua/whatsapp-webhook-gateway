"""Slice 1 (RED first): the team members the admin adds. Passwords are never stored in clear text."""
import asyncio
import os
import tempfile
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us


class UsersStore(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name

    def tearDown(self):
        if os.path.exists(self._file):
            os.remove(self._file)

    def run_(self, coro):
        return asyncio.run(coro)

    def test_created_user_can_log_in_with_the_right_password_only(self):
        self.run_(us.create_user(username="Mei", name="Mei Ling", password="correct horse 42"))
        user = self.run_(us.get_user("mei"))
        self.assertTrue(us.verify_password(user, "correct horse 42"))
        self.assertFalse(us.verify_password(user, "wrong password"))
        self.assertFalse(us.verify_password(user, ""))

    def test_password_is_stored_hashed_and_salted(self):
        self.run_(us.create_user(username="mei", name="Mei", password="correct horse 42"))
        self.run_(us.create_user(username="raj", name="Raj", password="correct horse 42"))
        raw = open(us._FILE, encoding="utf-8").read()
        self.assertNotIn("correct horse 42", raw)
        a, b = self.run_(us.get_user("mei")), self.run_(us.get_user("raj"))
        self.assertNotEqual(a["pw_hash"], b["pw_hash"], "same password must not give the same hash (per-user salt)")

    def test_usernames_are_normalised_unique_and_not_admin(self):
        self.run_(us.create_user(username="  Mei.L ", name="Mei", password="longenough1"))
        self.assertIsNotNone(self.run_(us.get_user("mei.l")))
        for bad in ("mei.l", "MEI.L"):
            with self.assertRaises(ValueError):
                self.run_(us.create_user(username=bad, name="Dup", password="longenough1"))
        for bad in ("admin", "ab", "has space", "x" * 40, ""):
            with self.assertRaises(ValueError):
                self.run_(us.create_user(username=bad, name="X", password="longenough1"))

    def test_short_passwords_are_refused(self):
        with self.assertRaises(ValueError):
            self.run_(us.create_user(username="mei", name="Mei", password="short"))

    def test_listing_never_exposes_hashes(self):
        self.run_(us.create_user(username="mei", name="Mei", password="longenough1"))
        row = self.run_(us.list_users())[0]
        self.assertEqual((row["username"], row["name"], row["disabled"]), ("mei", "Mei", False))
        self.assertNotIn("pw_hash", row); self.assertNotIn("salt", row)

    def test_disable_reset_password_and_delete(self):
        self.run_(us.create_user(username="mei", name="Mei", password="longenough1"))
        self.run_(us.update_user("mei", disabled=True))
        self.assertTrue(self.run_(us.get_user("mei"))["disabled"])
        self.run_(us.update_user("mei", password="a brand new pass", disabled=False))
        user = self.run_(us.get_user("mei"))
        self.assertTrue(us.verify_password(user, "a brand new pass")); self.assertFalse(us.verify_password(user, "longenough1"))
        self.run_(us.delete_user("mei"))
        self.assertIsNone(self.run_(us.get_user("mei")))


if __name__ == "__main__":
    unittest.main()
