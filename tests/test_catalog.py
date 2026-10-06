"""The stock list: every product page of the knowledge base with an on/off switch. Off = not in stock = the AI never
mentions it. This file covers the list, the names the AI recognises, storage and the admin API/page."""
import asyncio
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import catalog
import feedback_store as fs
import users_store as us
from fastapi.testclient import TestClient


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        fs._FILE = self._file = tmp.name
        catalog.invalidate()

    def tearDown(self):
        if os.path.exists(self._file): os.remove(self._file)
        catalog.invalidate()


class TheList(Base):
    def test_every_product_page_is_an_item_with_a_family(self):
        items = catalog.items()
        self.assertEqual(len(items), 50)
        fam = {}
        for i in items: fam[i.family] = fam.get(i.family, 0) + 1
        self.assertEqual(fam, {"bitumen": 8, "emulsion": 21, "oxidized": 19, "pmb": 1, "base-oil": 1})

    def test_names_are_readable_and_unique(self):
        titles = [i.title for i in catalog.items()]
        self.assertEqual(len(set(titles)), 50)
        for want in ("Bitumen 60/70", "Bitumen Emulsion CSS-1h", "Oxidized Bitumen R85/40", "Bitumen VG 30", "Polymer Modified Bitumen", "Base Oil SN150"):
            self.assertIn(want, titles)

    def test_overview_and_company_pages_are_not_switches(self):
        slugs = {i.slug for i in catalog.items()}
        for page in ("products-bitumen", "products-bitumen-emulsion", "products-oxidized-bitumen", "about", "contact", "resources-faq"):
            self.assertNotIn(page, slugs)

    def test_every_item_has_names_the_ai_can_recognise_and_none_overlap(self):
        seen = {}
        for i in catalog.items():
            self.assertTrue(i.aliases, i.slug)
            for a in i.aliases:
                self.assertNotIn(a, seen, f"{a} belongs to both {seen.get(a)} and {i.slug}")
                seen[a] = i.slug

    def find(self, text):
        return sorted(i.slug.replace("products-", "") for i in catalog.mentioned(text))

    def test_recognises_grades_written_in_different_ways(self):
        self.assertEqual(self.find("do you have 60/70?"), ["bitumen-60-70"])
        self.assertEqual(self.find("price for bitumen 60-70 please"), ["bitumen-60-70"])
        self.assertEqual(self.find("I need VG30 and vg 40"), ["bitumen-vg-30", "bitumen-vg-40"])
        self.assertEqual(self.find("CSS-1h emulsion"), ["bitumen-emulsion-css-1h"])
        self.assertEqual(self.find("css1h"), ["bitumen-emulsion-css-1h"])
        self.assertEqual(self.find("polymer modified bitumen / PMB"), ["polymer-modified-bitumen"])
        self.assertEqual(self.find("base oil SN150"), ["base-oil-sn150"])

    def test_similar_names_are_not_confused(self):
        self.assertEqual(self.find("SS-1"), ["bitumen-emulsion-ss-1"])
        self.assertEqual(self.find("CSS-1"), ["bitumen-emulsion-css-1"])
        self.assertEqual(self.find("HFMS-1"), ["bitumen-emulsion-hfms-1"])
        self.assertEqual(self.find("MS-1"), ["bitumen-emulsion-ms-1"])
        self.assertEqual(self.find("R75/25"), ["oxidized-r75-25"])
        self.assertEqual(self.find("75/25"), ["oxidized-75-25"])
        self.assertEqual(self.find("K1-40"), ["bitumen-emulsion-k1-40"])
        self.assertEqual(self.find("500 tonnes to Port Klang, 2026"), [])


class Storage(Base):
    def test_everything_is_on_until_switched_off(self):
        self.assertEqual(run(catalog.disabled_items()), [])
        self.assertTrue(run(catalog.is_on("products-bitumen-60-70")))

    def test_switching_an_item_off_and_on_is_remembered_with_who_and_when(self):
        run(catalog.set_item("products-bitumen-60-70", False, "Mei"))
        self.assertFalse(run(catalog.is_on("products-bitumen-60-70")))
        self.assertEqual([i.slug for i in run(catalog.disabled_items())], ["products-bitumen-60-70"])
        snap = run(catalog.snapshot())
        entry = [x for f in snap["families"] for x in f["items"] if x["slug"] == "products-bitumen-60-70"][0]
        self.assertEqual((entry["on"], entry["by"]), (False, "Mei")); self.assertTrue(entry["ts"])
        run(catalog.set_item("products-bitumen-60-70", True, "Mei"))
        self.assertEqual(run(catalog.disabled_items()), [])

    def test_unknown_items_are_refused(self):
        with self.assertRaises(ValueError): run(catalog.set_item("products-nope", False, "x"))
        with self.assertRaises(ValueError): run(catalog.set_family("nope", False, "x"))

    def test_a_family_switch_changes_all_its_items(self):
        n = run(catalog.set_family("oxidized", False, "Mei"))
        self.assertEqual(n, 19)
        off = run(catalog.disabled_items())
        self.assertEqual({i.family for i in off}, {"oxidized"}); self.assertEqual(len(off), 19)
        run(catalog.set_family("oxidized", True, "Mei")); self.assertEqual(run(catalog.disabled_items()), [])

    def test_pages_to_hide_from_the_ai(self):
        run(catalog.set_item("products-bitumen-60-70", False, "x"))
        self.assertEqual(run(catalog.hidden_slugs()), {"products-bitumen-60-70"})

    def test_a_family_overview_page_is_hidden_only_when_the_whole_family_is_off(self):
        run(catalog.set_item("products-bitumen-60-70", False, "x"))
        self.assertNotIn("products-bitumen", run(catalog.hidden_slugs()))
        run(catalog.set_family("bitumen", False, "x"))
        self.assertIn("products-bitumen", run(catalog.hidden_slugs()))

    def test_base_oil_has_no_page_of_its_own_so_its_page_is_never_hidden(self):
        run(catalog.set_item("products-base-oil-sn150", False, "x"))
        self.assertNotIn("products-base-oil-sn150", run(catalog.hidden_slugs()))   # that scraped "page" is the Contact page
        self.assertEqual([i.slug for i in run(catalog.disabled_items())], ["products-base-oil-sn150"])

    def test_a_change_is_seen_at_once_despite_the_cache(self):
        self.assertEqual(run(catalog.hidden_slugs()), set())
        run(catalog.set_item("products-bitumen-60-70", False, "x"))
        self.assertEqual(run(catalog.hidden_slugs()), {"products-bitumen-60-70"})


class AdminApiAndPage(Base):
    def setUp(self):
        super().setUp()
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._ufile = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False; main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1")); run(main._refresh_users_cache())
        self.admin = TestClient(main.app); self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.agent = TestClient(main.app); self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        self.audits = []
        self._a = mock.patch.object(main._sb, "audit", mock.AsyncMock(side_effect=lambda *a, **kw: self.audits.append((a, kw)))); self._a.start()

    def tearDown(self):
        self._a.stop()
        if os.path.exists(self._ufile): os.remove(self._ufile)
        super().tearDown()

    def test_only_the_admin_can_see_or_change_stock(self):
        anon = TestClient(self.main.app)
        for method, path, body in (("get", "/api/inbox/catalog", None), ("put", "/api/inbox/catalog/item/products-bitumen-60-70", {"on": False}),
                                   ("put", "/api/inbox/catalog/family/oxidized", {"on": False})):
            self.assertEqual(getattr(anon, method)(path, **({"json": body} if body else {})).status_code, 401, path)
            self.assertEqual(getattr(self.agent, method)(path, **({"json": body} if body else {})).status_code, 403, path)
        self.assertEqual(run(catalog.disabled_items()), [])

    def test_the_list_is_grouped_by_family_with_counts(self):
        j = self.admin.get("/api/inbox/catalog").json()
        self.assertEqual([f["id"] for f in j["families"]], ["bitumen", "emulsion", "oxidized", "pmb", "base-oil"])
        self.assertEqual([f["total"] for f in j["families"]], [8, 21, 19, 1, 1]); self.assertEqual(j["off_total"], 0)
        one = j["families"][0]["items"][0]; self.assertEqual(set(one) >= {"slug", "title", "on", "url", "by", "ts"}, True)

    def test_switching_an_item_off_is_saved_and_logged(self):
        r = self.admin.put("/api/inbox/catalog/item/products-bitumen-60-70", json={"on": False})
        self.assertEqual((r.status_code, r.json()["on"]), (200, False))
        j = self.admin.get("/api/inbox/catalog").json(); self.assertEqual(j["off_total"], 1)
        self.assertEqual(j["families"][0]["on_count"], 7)
        self.assertEqual(self.audits[-1][0][1], "catalog_toggle"); self.assertIn("Bitumen 60/70", str(self.audits[-1][1]["detail"]))

    def test_family_switch_and_bad_input(self):
        r = self.admin.put("/api/inbox/catalog/family/emulsion", json={"on": False}); self.assertEqual(r.json()["changed"], 21)
        self.assertEqual(self.admin.put("/api/inbox/catalog/item/products-nope", json={"on": False}).status_code, 404)
        self.assertEqual(self.admin.put("/api/inbox/catalog/family/nope", json={"on": False}).status_code, 404)
        self.assertEqual(self.admin.put("/api/inbox/catalog/item/products-bitumen-60-70", json={"on": "maybe"}).status_code, 422)

    def test_the_stock_page(self):
        html = self.admin.get("/inbox/stock").text
        for needle in ('id="stock-families"', 'id="stock-search"', 'id="stock-filter-off"', "/api/inbox/catalog"):
            self.assertIn(needle, html)
        self.assertIn('href="/inbox/stock"', html)                       # nav link for the admin
        r = self.agent.get("/inbox/stock", follow_redirects=False); self.assertEqual(r.status_code, 302)
        self.assertNotIn('href="/inbox/stock"', self.agent.get("/inbox/chats").text)


if __name__ == "__main__":
    unittest.main()
