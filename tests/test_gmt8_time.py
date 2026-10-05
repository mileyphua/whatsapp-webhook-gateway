"""Every time shown in the inbox is GMT+8 in 12-hour form (19:03 UTC is 3:03 AM the next day), not raw UTC."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "petrobind_frontend_app")


class ServerFilter(unittest.TestCase):
    def setUp(self):
        import main
        self.f = main._gmt8

    def test_utc_evening_becomes_gmt8_morning_of_the_next_day(self):
        self.assertEqual(self.f("2026-10-05T19:03:54Z"), "2026-10-06 3:03 AM")

    def test_seconds_on_request(self):
        self.assertEqual(self.f("2026-10-05T19:03:54Z", True), "2026-10-06 3:03:54 AM")

    def test_all_the_shapes_the_app_produces(self):
        for v in ("2026-10-05T19:03:54", "2026-10-05T19:03:54+00:00", "2026-10-05 19:03:54", "2026-10-05T19:03:54.123456Z", 1791227034 - 1791227034 + 1791227034):
            self.assertTrue(self.f(v).endswith("AM") or self.f(v).endswith("PM"), v)
        self.assertEqual(self.f("2026-10-05T19:03:54+00:00"), "2026-10-06 3:03 AM")
        self.assertEqual(self.f("2026-10-05T19:03:54.5Z"), "2026-10-06 3:03 AM")

    def test_twelve_hour_edges(self):
        self.assertEqual(self.f("2026-10-05T16:07:00Z"), "2026-10-06 12:07 AM")     # midnight GMT+8
        self.assertEqual(self.f("2026-10-06T04:07:00Z"), "2026-10-06 12:07 PM")     # noon GMT+8
        self.assertEqual(self.f("2026-10-06T05:07:00Z"), "2026-10-06 1:07 PM")

    def test_a_timezone_offset_is_respected(self):
        self.assertEqual(self.f("2026-10-06T03:03:54+08:00"), "2026-10-06 3:03 AM")  # already GMT+8

    def test_unix_seconds_and_bad_values(self):
        self.assertEqual(self.f(1790000000), "2026-09-21 10:13 PM")
        self.assertEqual((self.f(""), self.f(None), self.f("nonsense")), ("", "", "nonsense"))


class PagesShowGmt8(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear()
        self._en = sb.ENABLED; sb.ENABLED = False
        self._st = sb._LOCAL_OUTBOUND_FILE; sb._LOCAL_OUTBOUND_FILE = tempfile.mktemp(suffix=".json"); sb._LOCAL_OUTBOUND.clear()
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        sb.ENABLED = self._en
        if os.path.exists(sb._LOCAL_OUTBOUND_FILE): os.remove(sb._LOCAL_OUTBOUND_FILE)
        sb._LOCAL_OUTBOUND_FILE = self._st; sb._LOCAL_OUTBOUND.clear()
        if os.path.exists(self._file): os.remove(self._file)

    def test_the_thread_and_the_chat_list_show_gmt8(self):
        n = "60133000777"
        asyncio.run(self.main._persist_inbound_safe({"id": "w1", "from": n, "type": "text", "text": {"body": "hello"}, "timestamp": "1790000000"}))   # 2026-09-21 14:13:20 UTC
        thread = self.c.get(f"/inbox/chat/{n}").text
        self.assertIn("2026-09-21 10:13 PM", thread); self.assertNotIn("14:13", thread)
        lst = self.c.get("/inbox/chats").text
        self.assertIn("2026-09-21 10:13 PM", lst); self.assertNotIn("14:13", lst)

    def test_the_template_setup_does_not_fail_silently_at_start_up(self):
        self.assertEqual(self.main._JINJA_ENV.globals.get("tz_label"), "GMT+8")
        self.assertIn("gmt8", self.main._JINJA_ENV.filters)
        n = "60133000778"
        asyncio.run(self.main._persist_inbound_safe({"id": "w2", "from": n, "type": "text", "text": {"body": "hi"}, "timestamp": "1790000000"}))
        self.assertIn('title="GMT+8"', self.c.get(f"/inbox/chat/{n}").text)

    def test_pages_load_the_browser_time_helper_before_app_js(self):
        html = self.c.get("/inbox/chats").text
        self.assertIn("/inbox/static/time.js", html); self.assertLess(html.index("time.js"), html.index("app.js"))
        self.assertEqual(self.c.get("/inbox/static/time.js").status_code, 200)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BrowserHelper(unittest.TestCase):
    def test_browser_helper_matches_the_server(self):
        r = subprocess.run(["node", os.path.join(HERE, "pbtime_check.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(o["evening_utc_to_next_morning"], "2026-10-06 3:03 AM")
        self.assertEqual(o["with_seconds"], "2026-10-06 3:03:54 AM")
        self.assertEqual((o["naive_server_string_is_utc"], o["offset_string"], o["space_separated"]), ("2026-10-06 3:03 AM",) * 3)
        self.assertEqual(o["date_object"], "2026-10-06 12:05 PM")
        self.assertEqual((o["midnight"], o["noon"]), ("2026-10-06 12:07 AM", "2026-10-06 12:07 PM"))
        self.assertEqual((o["time_only"], o["time_only_seconds"]), ("3:03 AM", "3:03:54 AM"))
        self.assertEqual(o["bad"], ["", "", "nonsense"]); self.assertEqual(o["label"], "GMT+8")


class NoRawUtcLeftInTheScripts(unittest.TestCase):
    """Every place that printed a UTC/browser-zone time now goes through the helper."""
    def read(self, *p):
        return open(os.path.join(ROOT, *p), encoding="utf-8").read()

    def test_no_raw_iso_time_strings_are_shown_to_people(self):
        app = self.read("static", "app.js")
        self.assertNotIn('toISOString().replace("T", " ")', app)
        self.assertNotIn("toLocaleTimeString", app)
        self.assertNotIn("toLocaleTimeString", self.read("static", "admin.js"))
        self.assertNotIn('replace("T", " ")', self.read("static", "admin.js"))
        self.assertNotIn('replace("T", " ")', self.read("templates", "logs.html"))
        self.assertNotIn('toISOString().replace("T", " ")', self.read("templates", "learning.html"))
        self.assertNotIn("replace('T', ' ')", self.read("templates", "chat_thread.html") + self.read("templates", "chat_list.html"))

    def test_ops_clock_is_gmt8_not_utc(self):
        self.assertNotIn("UTC\"", self.read("static", "admin.js").split("server-utc")[1][:600])


if __name__ == "__main__":
    unittest.main()
