"""A message sent from localhost must reach the database that Render reads. Localhost has no Supabase key on purpose,
so it hands each sent message to the Render inbox (logged in as admin), which saves it in Supabase."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

N = "60172630388"
ROW = {"e164": N, "direction": "human", "text": "Hi Kenneth, this is Sarah from Petrobind Global.", "wamid": "wamid.LOCAL1",
       "created_at": "2026-10-05T19:50:59Z", "sent_by": "Petrobind Admin"}


class FakeDb:
    posted = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers=None, json=None, **k):
        FakeDb.posted.append((url.rsplit("/", 1)[-1], headers, json))
        class R: status_code = 201
        return R()


class RenderImportEndpoint(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        FakeDb.posted = []
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(main._refresh_users_cache())
        self.admin = TestClient(main.app); self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.agent = TestClient(main.app); self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        self._p = [mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_client", lambda: FakeDb())]
        for p in self._p: p.start()

    def tearDown(self):
        for p in self._p: p.stop()
        if os.path.exists(self._file): os.remove(self._file)

    def test_saves_the_message_with_its_original_time_and_sender(self):
        r = self.admin.post("/api/inbox/admin/import-messages", json={"messages": [ROW]})
        self.assertEqual((r.status_code, r.json()["saved"]), (200, 1), r.text)
        table, headers, body = FakeDb.posted[0]
        self.assertEqual(table, "messages"); self.assertIn("ignore-duplicates", headers["Prefer"])
        self.assertEqual((body["wamid"], body["e164"], body["direction"], body["created_at"]), ("wamid.LOCAL1", N, "human", "2026-10-05T19:50:59Z"))
        self.assertEqual(body["payload_jsonb"]["sent_by"], "Petrobind Admin")

    def test_files_keep_their_name_and_media_id(self):
        row = {**ROW, "wamid": "wamid.LOCAL2", "media_type": "document", "filename": "specs.pdf", "media_id": "M1", "mime": "application/pdf"}
        self.admin.post("/api/inbox/admin/import-messages", json={"messages": [row]})
        body = FakeDb.posted[0][2]
        self.assertEqual(body["media_type"], "document"); self.assertEqual((body["payload_jsonb"]["filename"], body["payload_jsonb"]["media_id"]), ("specs.pdf", "M1"))

    def test_only_the_admin_may_use_it(self):
        self.assertEqual(self.agent.post("/api/inbox/admin/import-messages", json={"messages": [ROW]}).status_code, 403)
        self.assertEqual(TestClient(self.main.app).post("/api/inbox/admin/import-messages", json={"messages": [ROW]}).status_code, 401)
        self.assertEqual(FakeDb.posted, [])

    def test_bad_rows_are_skipped_and_the_batch_is_capped(self):
        rows = [ROW, {"e164": "", "direction": "human", "text": "x", "wamid": "w"}, {**ROW, "direction": "banana", "wamid": "w2"}, {**ROW, "wamid": ""}]
        r = self.admin.post("/api/inbox/admin/import-messages", json={"messages": rows})
        self.assertEqual((r.json()["saved"], r.json()["skipped"]), (1, 3))
        self.assertEqual(self.admin.post("/api/inbox/admin/import-messages", json={"messages": [ROW] * 201}).status_code, 413)

    def test_without_supabase_it_says_so(self):
        with mock.patch.object(sb, "ENABLED", False):
            self.assertEqual(self.admin.post("/api/inbox/admin/import-messages", json={"messages": [ROW]}).status_code, 503)


class FakeRender:
    """Stands in for the Render inbox as seen from localhost."""
    calls = []
    fail = False

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, data=None, headers=None, cookies=None, **k):
        FakeRender.calls.append((url, json, data))
        class R:
            status_code = 200; headers = {}; text = "{}"
            def __init__(s, code=200, body=None): s.status_code = code; s._b = body or {}; s.cookies = {"inbox_session": "S"}
            def json(s): return s._b
        if FakeRender.fail: raise ConnectionError("render is asleep")
        if url.endswith("/inbox/login"): return R(302)
        return R(200, {"saved": len(json["messages"]), "skipped": 0})


class LocalhostForwardsToRender(unittest.TestCase):
    def setUp(self):
        FakeRender.calls = []; FakeRender.fail = False
        import main
        self.main = main
        self._en = sb.ENABLED; sb.ENABLED = False
        self._st = sb._LOCAL_OUTBOUND_FILE; sb._LOCAL_OUTBOUND_FILE = tempfile.mktemp(suffix=".json"); sb._LOCAL_OUTBOUND.clear()
        sb._RENDER_COOKIE = None
        self._p = [mock.patch.dict(os.environ, {"RENDER_INBOX_URL": "https://render.example", "INBOX_ADMIN_TOKEN": "tok-123456"}),
                   mock.patch.object(sb.httpx, "AsyncClient", FakeRender)]
        for p in self._p: p.start()

    def tearDown(self):
        for p in self._p: p.stop()
        sb.ENABLED = self._en
        if os.path.exists(sb._LOCAL_OUTBOUND_FILE): os.remove(sb._LOCAL_OUTBOUND_FILE)
        sb._LOCAL_OUTBOUND_FILE = self._st; sb._LOCAL_OUTBOUND.clear(); sb._RENDER_COOKIE = None

    def send(self, text="hello", sid="wamid.L9", **kw):
        asyncio.run(self.main._persist_outbound_safe(e164=N, direction="human", text=text, sent_id_from_graph=sid, sent_by="Petrobind Admin", **kw))

    def imports(self):
        return [c for c in FakeRender.calls if c[0].endswith("/api/inbox/admin/import-messages")]

    def test_a_sent_message_is_forwarded_to_render(self):
        self.send()
        self.assertEqual(len(self.imports()), 1)
        row = self.imports()[0][1]["messages"][0]
        self.assertEqual((row["wamid"], row["e164"], row["direction"], row["text"], row["sent_by"]), ("wamid.L9", N, "human", "hello", "Petrobind Admin"))
        self.assertEqual(sb.local_unsynced_count(), 0)

    def test_it_logs_in_once_and_reuses_the_session(self):
        self.send(sid="wamid.A"); self.send(sid="wamid.B")
        self.assertEqual(len([c for c in FakeRender.calls if c[0].endswith("/inbox/login")]), 1)

    def test_if_render_is_unreachable_the_message_is_kept_and_sent_later(self):
        FakeRender.fail = True
        self.send(sid="wamid.LATE")
        self.assertEqual(sb.local_unsynced_count(), 1)
        FakeRender.fail = False; sb._RENDER_COOKIE = None
        n = asyncio.run(sb.sync_pending_local())
        self.assertEqual(n, 1); self.assertEqual(sb.local_unsynced_count(), 0)
        self.assertEqual(self.imports()[-1][1]["messages"][0]["wamid"], "wamid.LATE")

    def test_nothing_is_sent_twice(self):
        self.send(sid="wamid.ONCE"); asyncio.run(sb.sync_pending_local()); asyncio.run(sb.sync_pending_local())
        self.assertEqual(len(self.imports()), 1)

    def test_copies_of_ai_history_and_notes_without_a_whatsapp_id_are_not_forwarded(self):
        self.send(text="a system note", sid=None)
        self.assertEqual(self.imports(), [])
        self.assertEqual(sb.local_unsynced_count(), 0)

    def test_no_forwarding_unless_a_render_address_is_configured(self):
        with mock.patch.dict(os.environ, {"RENDER_INBOX_URL": ""}):
            self.send(sid="wamid.NOPE")
            self.assertEqual(sb.local_unsynced_count(), 0)
        self.assertEqual(FakeRender.calls, [])

    def test_a_server_with_supabase_writes_directly_and_never_forwards(self):
        sb.ENABLED = True
        with mock.patch.object(sb, "_client", lambda: FakeDb()):
            self.send(sid="wamid.DIRECT")
        self.assertEqual(self.imports(), [])


if __name__ == "__main__":
    unittest.main()
