"""The product pages on the website must be reachable before we show or send their links. (The live site once answered
404 for every product page: a stale deployment.)"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import catalog
import feedback_store as fs
import site_links
import users_store as us
from fastapi.testclient import TestClient

GOOD = "https://www.petrobindglobal.com/products/bitumen-60-70"
BAD = "https://www.petrobindglobal.com/products/bitumen-80-100"


def run(c):
    return asyncio.run(c)


class FakeWeb:
    calls = []
    status = {GOOD: 200}
    boom = False

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, **k):
        FakeWeb.calls.append(url)
        if FakeWeb.boom: raise ConnectionError("offline")
        class R:
            status_code = FakeWeb.status.get(url, 404); headers = {"content-type": "text/html; charset=utf-8"}
        return R()


class Checker(unittest.TestCase):
    def setUp(self):
        FakeWeb.calls, FakeWeb.status, FakeWeb.boom = [], {GOOD: 200}, False
        site_links.clear()
        self._p = mock.patch.object(site_links.httpx, "AsyncClient", FakeWeb); self._p.start()

    def tearDown(self):
        self._p.stop(); site_links.clear()

    def test_a_working_page_is_live_and_a_404_is_not(self):
        self.assertTrue(run(site_links.is_live(GOOD))); self.assertFalse(run(site_links.is_live(BAD)))
        r = run(site_links.check(BAD)); self.assertEqual((r["ok"], r["status"]), (False, 404))

    def test_results_are_cached_so_a_busy_chat_does_not_hammer_the_website(self):
        run(site_links.is_live(GOOD)); run(site_links.is_live(GOOD)); run(site_links.is_live(GOOD))
        self.assertEqual(FakeWeb.calls.count(GOOD), 1)

    def test_a_bad_result_is_rechecked_sooner_than_a_good_one(self):
        run(site_links.is_live(BAD))
        site_links._cache[BAD] = (time.time() - 200, site_links._cache[BAD][1])      # older than the short failure window
        FakeWeb.status[BAD] = 200
        self.assertTrue(run(site_links.is_live(BAD)))                                  # fixed site: recovers quickly

    def test_when_the_website_cannot_be_reached_at_all_we_do_not_block_links(self):
        FakeWeb.boom = True
        self.assertTrue(run(site_links.is_live(GOOD)))                                 # unknown is not the same as dead
        self.assertIsNone(run(site_links.check(GOOD))["ok"])

    def test_many_at_once(self):
        res = run(site_links.check_many([GOOD, BAD, GOOD]))
        self.assertEqual({u: r["ok"] for u, r in res.items()}, {GOOD: True, BAD: False})


class StockPageForEveryone(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name); fs._FILE = self._f = tmp.name
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name); us._FILE = self._u = tmp.name
        catalog.invalidate(); site_links.clear()
        FakeWeb.calls, FakeWeb.status, FakeWeb.boom = [], {GOOD: 200}, False
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24; main._sb.ENABLED = False; main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1")); run(main._refresh_users_cache())
        self.admin = TestClient(main.app); self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.agent = TestClient(main.app); self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        run(catalog.set_item("products-bitumen-50-70", False, "Admin"))
        self._p = mock.patch.object(site_links.httpx, "AsyncClient", FakeWeb); self._p.start()

    def tearDown(self):
        self._p.stop(); catalog.invalidate(); site_links.clear()
        for f in (self._f, self._u):
            if os.path.exists(f): os.remove(f)

    def test_team_members_can_see_availability_but_not_who_or_when(self):
        j = self.agent.get("/api/inbox/catalog").json()
        item = [i for f in j["families"] for i in f["items"] if i["slug"] == "products-bitumen-50-70"][0]
        self.assertFalse(item["on"]); self.assertNotIn("by", item); self.assertNotIn("ts", item)
        self.assertEqual(j["off_total"], 1)
        a = self.admin.get("/api/inbox/catalog").json()
        self.assertEqual([i for f in a["families"] for i in f["items"] if i["slug"] == "products-bitumen-50-70"][0]["by"], "Admin")

    def test_only_the_admin_can_change_stock(self):
        self.assertEqual(self.agent.put("/api/inbox/catalog/item/products-bitumen-50-70", json={"on": True}).status_code, 403)
        self.assertEqual(self.agent.put("/api/inbox/catalog/family/bitumen", json={"on": True}).status_code, 403)
        self.assertEqual(TestClient(self.main.app).get("/api/inbox/catalog").status_code, 401)

    def test_the_page_for_a_team_member_is_read_only_without_the_description(self):
        html = self.agent.get("/inbox/stock")
        self.assertEqual(html.status_code, 200); h = html.text
        self.assertIn('id="stock-families"', h); self.assertIn("var IS_ADMIN = false", h)
        self.assertNotIn("Switch an item <b>Off</b>", h)                               # the explanation is for the admin only
        self.assertIn('href="/inbox/stock"', h)                                        # and the menu link is there
        a = self.admin.get("/inbox/stock").text
        self.assertIn("var IS_ADMIN = true", a); self.assertIn("Switch an item <b>Off</b>", a)

    def test_link_status_for_every_item_is_available_to_all_logged_in_users(self):
        for client in (self.admin, self.agent):
            j = client.get("/api/inbox/catalog/links").json()
            self.assertEqual(j["links"]["products-bitumen-60-70"]["ok"], True)
            self.assertEqual(j["links"]["products-bitumen-80-100"]["ok"], False)
            self.assertGreaterEqual(j["broken"], 40); self.assertEqual(j["checked"], j["broken"] + j["working"])
        self.assertEqual(TestClient(self.main.app).get("/api/inbox/catalog/links").status_code, 401)


class TheAiNeverSendsADeadLink(unittest.TestCase):
    def test_yes_to_know_more_with_a_dead_page_hands_over_instead_of_sending_it(self):
        import conversation_store as cs
        import llm_assistant as L
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_dead_link.json"; catalog.invalidate(); site_links.clear()
        FakeWeb.calls, FakeWeb.status, FakeWeb.boom = [], {}, False                    # every page 404s
        s = cs.ConversationSession(phone_number="60177700088"); s.message_count = 3
        s.more_info_url, s.more_info_title, s.more_info_at = BAD, "Bitumen 80/100", time.time()
        cs._SESSIONS["60177700088"] = s
        with mock.patch.object(site_links.httpx, "AsyncClient", FakeWeb):
            out = run(L.handle_incoming_message(phone_number="60177700088", inbound_text="yes please", draft_only=False))
        self.assertNotIn("petrobindglobal.com/products", out)
        self.assertIn("datasheet", out.lower()); self.assertIsNotNone(s.needs_human_since)
        self.assertEqual(s.more_info_url, "")


if __name__ == "__main__":
    unittest.main()
