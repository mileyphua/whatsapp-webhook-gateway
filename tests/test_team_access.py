"""Slice 3 (RED first): what a team member may and may not reach. Members see only the inbox."""
import asyncio
import os
import tempfile
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import learning
import users_store as us
from unittest import mock
from fastapi.testclient import TestClient

# (method, path, json body) a team member must be refused (403) or redirected away from
ADMIN_ONLY_API = [
    ("GET", "/api/inbox/logs", None),
    ("GET", "/api/inbox/learning", None),
    ("POST", "/api/inbox/learning/learn", {}),
    ("POST", "/api/inbox/learning/skills", {"name": "x", "description": "y", "instructions": "z"}),
    ("PUT", "/api/inbox/learning/skills/abc", {"name": "x", "description": "y", "instructions": "z"}),
    ("PATCH", "/api/inbox/learning/skills/abc", {"status": "active"}),
    ("DELETE", "/api/inbox/learning/skills/abc", None),
    ("PATCH", "/api/inbox/learning/lessons/abc", {"status": "disabled"}),
    ("DELETE", "/api/inbox/learning/lessons/abc", None),
    ("GET", "/api/inbox/admin/dashboard", None),
    ("POST", "/api/inbox/admin/import-history", {}),
    ("POST", "/api/inbox/admin/force-redis-scan", {}),
    ("POST", "/api/inbox/admin/evict-templates-cache", {}),
    ("POST", "/api/inbox/admin/run-followups-scan", {}),
    ("POST", "/api/inbox/admin/flush-scheduled-sends", {}),
    ("POST", "/api/inbox/chats/60123456789/forget-memory", None),
    ("DELETE", "/api/inbox/templates/cache", None),
]
# things a member needs in order to read and reply (must NOT be 401/403)
MEMBER_API = [
    ("GET", "/api/inbox/me", None),
    ("GET", "/api/inbox/chats", None),
    ("GET", "/api/inbox/chats/60123456789/messages", None),
    ("GET", "/api/inbox/chats/60123456789/summary", None),
    ("GET", "/api/inbox/chats/60123456789/claim", None),
    ("POST", "/api/inbox/chats/60123456789/claim", {"ttl_seconds": 60}),
    ("DELETE", "/api/inbox/chats/60123456789/claim", None),
    ("GET", "/api/inbox/window-check/60123456789", None),
    ("GET", "/api/inbox/handoffs", None),
    ("POST", "/api/inbox/feedback", {"e164": "1", "ai_text": "x", "rating": "up"}),
    ("GET", "/api/inbox/feedback", None),
    ("PUT", "/api/inbox/chats/60123456789/name", {"name": "Test"}),
]
# these really send messages / hit Redis when an admin calls them, so the "admin is allowed" check skips them
HEAVY = {"/api/inbox/admin/run-followups-scan", "/api/inbox/admin/flush-scheduled-sends", "/api/inbox/admin/force-redis-scan",
         "/api/inbox/admin/import-history", "/api/inbox/learning/learn"}
ADMIN_ONLY_PAGES = ["/inbox/admin", "/inbox/logs", "/inbox/learning", "/inbox/architecture", "/inbox/guide", "/inbox/team"]


class Access(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        self._no_net = mock.patch.object(learning, "_llm_json", mock.AsyncMock(return_value=None))
        self._no_net.start()
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

    def tearDown(self):
        self._no_net.stop()
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def call(self, c, method, path, body):
        return c.request(method, path, json=body) if body is not None else c.request(method, path)

    def test_member_is_refused_every_admin_only_api(self):
        for m, p, b in ADMIN_ONLY_API:
            self.assertEqual(self.call(self.agent, m, p, b).status_code, 403, f"{m} {p}")

    def test_admin_is_not_refused_those_apis(self):
        for m, p, b in ADMIN_ONLY_API:
            if p in HEAVY:
                continue
            self.assertNotIn(self.call(self.admin, m, p, b).status_code, (401, 403), f"{m} {p}")

    def test_member_can_use_everything_needed_to_read_and_reply(self):
        for m, p, b in MEMBER_API:
            self.assertNotIn(self.call(self.agent, m, p, b).status_code, (401, 403), f"{m} {p}")

    def test_member_sees_the_inbox_pages(self):
        self.assertEqual(self.agent.get("/inbox/chats").status_code, 200)
        self.assertEqual(self.agent.get("/inbox/chat/60123456789").status_code, 200)

    def test_member_is_sent_back_to_the_inbox_from_every_other_page(self):
        for page in ADMIN_ONLY_PAGES:
            r = self.agent.get(page, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers.get("location")), (302, "/inbox/chats"), page)

    def test_admin_still_sees_every_page(self):
        for page in [p for p in ADMIN_ONLY_PAGES if p != "/inbox/team"]:
            self.assertEqual(self.admin.get(page).status_code, 200, page)

    def test_a_member_does_not_see_other_sections_in_the_navigation(self):
        html = self.agent.get("/inbox/chats").text
        for label in ("/inbox/logs", "/inbox/learning", "/inbox/architecture", "/inbox/admin", "/inbox/guide", "/inbox/team"):
            self.assertNotIn('href="%s"' % label, html, label)
        self.assertIn("Mei Ling", html)                         # logged in as the member, not "Petrobind Admin"
        self.assertNotIn("Forget AI memory", html.split('id="act-forget"')[0][-200:] if 'id="act-forget"' in html else "")

    def test_admin_navigation_still_has_every_section(self):
        html = self.admin.get("/inbox/chats").text
        for label in ("/inbox/logs", "/inbox/learning", "/inbox/architecture", "/inbox/admin", "/inbox/guide", "/inbox/team"):
            self.assertIn('href="%s"' % label, html, label)

    def test_member_menu_can_delete_but_not_forget_ai_memory(self):
        html = self.agent.get("/inbox/chats").text
        self.assertNotRegex(html, r'id="act-delete"[^>]*\bhidden\b')
        self.assertRegex(html, r'id="act-forget"[^>]*\bhidden\b')

    def test_api_still_lets_a_member_set_a_name_but_attribution_uses_their_name(self):
        r = self.agent.get("/api/inbox/me").json()
        self.assertEqual((r["role"], r["name"]), ("agent", "Mei Ling"))


if __name__ == "__main__":
    unittest.main()
