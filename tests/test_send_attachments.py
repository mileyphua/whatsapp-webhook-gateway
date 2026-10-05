"""Slice 2 (RED first): send an image or document to a buyer through the WhatsApp Cloud API.
Meta flow: upload the file to /{phone-number-id}/media -> get an id -> send a message that points at that id."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

E = "60123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
PDF = b"%PDF-1.7\n" + b"x" * 300


class FakeMeta:
    calls = []
    fail_upload = False

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers=None, json=None, data=None, files=None, **k):
        class R:
            def __init__(s, status, body): s.status_code = status; s._b = body; s.content = b"{}"; s.text = str(body)
            def json(s): return s._b
        if url.endswith("/media"):
            FakeMeta.calls.append(("media", {"data": data, "file": (files["file"][0], files["file"][2], len(files["file"][1])), "auth": (headers or {}).get("Authorization")}))
            return R(400, {"error": {"message": "Param file must be a file with a valid mime type"}}) if FakeMeta.fail_upload else R(200, {"id": "MEDIA123"})
        FakeMeta.calls.append(("messages", json))
        return R(200, {"messages": [{"id": "wamid.MEDIA-SENT"}]})


class SendAttachments(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        FakeMeta.calls = []; FakeMeta.fail_upload = False
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        asyncio.run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        asyncio.run(main._refresh_users_cache())
        sb._LOCAL_CLAIMS.clear()
        self.saved = []

        async def record(**kw):
            self.saved.append(kw)

        self._patches = [
            mock.patch.object(main._sb, "ENABLED", False),
            mock.patch.object(main, "_sb_enabled", lambda: True),
            mock.patch.object(main._sb, "insert_outbound_message", record),
            mock.patch.object(main._sb, "audit", mock.AsyncMock()),
            mock.patch.object(main.httpx, "AsyncClient", FakeMeta),
            mock.patch.object(main, "ACCESS_TOKEN", "meta-token"),
            mock.patch.object(main, "PHONE_NUMBER_ID", "1055"),
        ]
        for p in self._patches:
            p.start()
        self.admin = self.login("", "x" * 24)
        self.mei = self.login("mei", "mei-password-1")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        sb._LOCAL_CLAIMS.clear()
        if os.path.exists(self._file):
            os.remove(self._file)
        self.main._USERS_CACHE.clear()

    def login(self, user, pw):
        c = TestClient(self.main.app)
        c.post("/inbox/login", data={"username": user, "password": pw}, follow_redirects=False)
        return c

    def send(self, c, name, data, ctype, **form):
        return c.post(f"/api/inbox/chats/{E}/attachments", files={"file": (name, data, ctype)}, data=form)

    def meta(self, kind):
        return [c[1] for c in FakeMeta.calls if c[0] == kind]

    def test_image_is_uploaded_then_sent_with_its_caption(self):
        r = self.send(self.admin, "loading-site.png", PNG, "image/png", caption="Loading at Port Klang")
        self.assertEqual((r.status_code, r.json()["success"]), (200, True))
        up = self.meta("media")[0]
        self.assertEqual((up["data"]["messaging_product"], up["data"]["type"], up["file"][0], up["file"][1], up["auth"]),
                         ("whatsapp", "image/png", "loading-site.png", "image/png", "Bearer meta-token"))
        self.assertEqual(self.meta("messages")[0], {"messaging_product": "whatsapp", "recipient_type": "individual", "to": E, "type": "image",
                                                     "image": {"id": "MEDIA123", "caption": "Loading at Port Klang"}})

    def test_document_keeps_its_file_name(self):
        self.send(self.admin, "COA 60-70.pdf", PDF, "application/pdf", caption="Latest COA")
        self.assertEqual(self.meta("messages")[0]["document"], {"id": "MEDIA123", "caption": "Latest COA", "filename": "COA 60-70.pdf"})
        self.assertEqual(self.meta("messages")[0]["type"], "document")

    def test_no_caption_means_no_caption_field_and_quote_is_optional(self):
        self.send(self.admin, "a.png", PNG, "image/png")
        self.assertEqual(self.meta("messages")[0]["image"], {"id": "MEDIA123"})
        self.assertNotIn("context", self.meta("messages")[0])
        self.send(self.admin, "b.png", PNG, "image/png", reply_to_wamid="wamid.BUYER1")
        self.assertEqual(self.meta("messages")[1]["context"], {"message_id": "wamid.BUYER1"})

    def test_the_sent_file_is_saved_to_the_inbox_with_who_sent_it(self):
        self.send(self.mei, "COA.pdf", PDF, "application/pdf", caption="Here you go")
        row = self.saved[0]
        self.assertEqual((row["direction"], row["media_type"], row["sent_by"], row["text"], row["sent_id_from_graph"]),
                         ("human", "document", "Mei Ling", "Here you go", "wamid.MEDIA-SENT"))
        self.assertEqual((row["media_meta"]["filename"], row["media_meta"]["media_id"]), ("COA.pdf", "MEDIA123"))

    def test_a_file_without_caption_is_listed_by_its_name(self):
        self.send(self.admin, "COA.pdf", PDF, "application/pdf")
        self.assertIn("COA.pdf", self.saved[0]["text"])

    def test_unsafe_or_unsupported_files_are_refused_and_nothing_is_sent(self):
        for name, data, ctype in (("run.exe", PDF, "application/octet-stream"), ("fake.pdf", PNG, "application/pdf"), ("empty.pdf", b"", "application/pdf")):
            r = self.send(self.admin, name, data, ctype)
            self.assertEqual(r.status_code, 422, name)
            self.assertTrue(r.json()["detail"])
        self.assertEqual(FakeMeta.calls, [])

    def test_a_missing_file_is_a_clear_error(self):
        r = self.admin.post(f"/api/inbox/chats/{E}/attachments", data={"caption": "no file"})
        self.assertEqual(r.status_code, 422)

    def test_a_chat_held_by_someone_else_cannot_receive_attachments(self):
        self.mei.post(f"/api/inbox/chats/{E}/claim", json={"ttl_seconds": 120})
        r = self.send(self.admin, "a.png", PNG, "image/png")
        self.assertEqual((r.status_code, r.json()["held_by"]), (409, "Mei Ling"))
        self.assertEqual(FakeMeta.calls, [])

    def test_sending_holds_the_chat_in_your_name(self):
        self.send(self.mei, "a.png", PNG, "image/png")
        self.assertEqual(sb._LOCAL_CLAIMS[E]["held_by"], "Mei Ling")

    def test_team_members_may_send_but_strangers_may_not(self):
        self.assertEqual(self.send(self.mei, "a.png", PNG, "image/png").status_code, 200)
        anon = TestClient(self.main.app)
        r = anon.post(f"/api/inbox/chats/{E}/attachments", files={"file": ("a.png", PNG, "image/png")})
        self.assertEqual(r.status_code, 401)

    def test_a_whatsapp_upload_error_is_reported_with_its_reason(self):
        FakeMeta.fail_upload = True
        r = self.send(self.admin, "a.png", PNG, "image/png")
        self.assertEqual(r.status_code, 502)
        self.assertIn("valid mime type", r.json()["error"])
        self.assertEqual(self.meta("messages"), [])
        self.assertEqual(self.saved, [])

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        with mock.patch("media_rules.MAX_DOCUMENT_BYTES", 1000), mock.patch.object(self.main.media_rules, "MAX_DOCUMENT_BYTES", 1000):
            r = self.send(self.admin, "big.pdf", PDF + b"x" * 5000, "application/pdf")
        self.assertIn(r.status_code, (413, 422))
        self.assertEqual(FakeMeta.calls, [])


if __name__ == "__main__":
    unittest.main()
