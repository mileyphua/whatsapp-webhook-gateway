"""Photos, voice notes and documents (received AND sent) are fetched from WhatsApp through the server, so the
access token never reaches the browser and only logged-in team members can see them."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeGraph:
    calls = []
    meta = {"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", "mime_type": "image/png", "file_size": 72}
    blob = PNG
    blob_type = "image/png"

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    @staticmethod
    def _r(status, body=None, content=b"", ctype="application/json"):
        class R:
            status_code = status; text = str(body); headers = {"content-type": ctype}
            def json(s): return body
        R.content = content if content else b"{}"
        return R()

    async def get(self, url, headers=None, params=None, **k):
        FakeGraph.calls.append((url, (headers or {}).get("Authorization")))
        if url.startswith("https://graph.facebook.com/"):
            if url.endswith("/BADID1"):
                return self._r(404, {"error": {"message": "Unsupported get request"}})
            return self._r(200, FakeGraph.meta)
        return self._r(200, None, FakeGraph.blob, FakeGraph.blob_type)


class MediaProxy(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        FakeGraph.calls = []
        FakeGraph.meta = {"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", "mime_type": "image/png", "file_size": 72}
        FakeGraph.blob, FakeGraph.blob_type = PNG, "image/png"
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear()
        self._p = [mock.patch.object(main, "ACCESS_TOKEN", "tok"), mock.patch.object(main.httpx, "AsyncClient", FakeGraph)]
        for p in self._p: p.start()
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        for p in self._p: p.stop()
        if os.path.exists(self._file): os.remove(self._file)

    def test_image_is_fetched_with_the_token_and_returned(self):
        r = self.c.get("/api/inbox/media/IMG123456")
        self.assertEqual((r.status_code, r.content, r.headers["content-type"]), (200, PNG, "image/png"))
        self.assertEqual(FakeGraph.calls[0][1], "Bearer tok"); self.assertEqual(FakeGraph.calls[1][1], "Bearer tok")
        self.assertIn("private", r.headers["cache-control"]); self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertTrue(r.headers.get("content-disposition", "inline").startswith("inline"))

    def test_login_is_required(self):
        self.assertEqual(TestClient(self.main.app).get("/api/inbox/media/IMG123456").status_code, 401)
        self.assertEqual(FakeGraph.calls, [])

    def test_only_plain_media_ids_are_accepted(self):
        for bad in ("a", "..%2F..%2Fetc", "x" * 200, "id;rm", "a b c d e"):
            self.assertIn(self.c.get(f"/api/inbox/media/{bad}").status_code, (400, 404), bad)
        self.assertEqual(FakeGraph.calls, [])

    def test_the_token_is_never_sent_to_a_foreign_host(self):
        FakeGraph.meta = {"url": "https://evil.example.com/steal", "mime_type": "image/png"}
        r = self.c.get("/api/inbox/media/IMG123456")
        self.assertEqual(r.status_code, 502)
        self.assertEqual([u for u, _ in FakeGraph.calls if "evil" in u], [])

    def test_a_page_that_could_run_script_is_never_served_inline(self):
        FakeGraph.blob, FakeGraph.blob_type = b"<script>alert(1)</script>", "text/html"
        r = self.c.get("/api/inbox/media/DOC1234567")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("html", r.headers["content-type"]); self.assertTrue(r.headers["content-disposition"].startswith("attachment"))

    def test_pdf_and_voice_note_play_inline(self):
        FakeGraph.blob, FakeGraph.blob_type = b"%PDF-1.7 x", "application/pdf"
        self.assertEqual(self.c.get("/api/inbox/media/PDF1234567").headers["content-type"], "application/pdf")
        FakeGraph.blob, FakeGraph.blob_type = b"OggS" + b"0" * 20, "audio/ogg; codecs=opus"
        r = self.c.get("/api/inbox/media/AUD1234567"); self.assertTrue(r.headers["content-type"].startswith("audio/ogg"))

    def test_whatsapp_saying_no_gives_a_clear_error(self):
        r = self.c.get("/api/inbox/media/BADID1")
        self.assertEqual(r.status_code, 404); self.assertIn("expire", r.json()["detail"].lower())

    def test_oversized_files_are_refused(self):
        FakeGraph.meta = {"url": "https://lookaside.fbsbx.com/x", "mime_type": "video/mp4", "file_size": 200 * 1024 * 1024}
        self.assertEqual(self.c.get("/api/inbox/media/VID1234567").status_code, 413)


class ThreadRendersMedia(unittest.TestCase):
    def test_server_page_shows_images_audio_documents_through_the_proxy(self):
        import asyncio
        import conversation_store
        import supabase_client as sb
        us._REDIS_URL = us._REDIS_TOKEN = ""
        import main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        store, enabled = sb._LOCAL_OUTBOUND_FILE, sb.ENABLED
        sb._LOCAL_OUTBOUND_FILE = tempfile.mktemp(suffix=".json"); sb.ENABLED = False; sb._LOCAL_OUTBOUND.clear()
        try:
            main._LOGIN_FAILS.clear()
            c = TestClient(main.app); c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
            n = "60155000444"
            for i, (t, extra) in enumerate([("image", {"caption": "site photo"}), ("audio", {}), ("document", {"filename": "po.pdf"})]):
                asyncio.run(main._persist_inbound_safe({"id": f"w{i}", "from": n, "type": t, t: {"id": f"MID{t}12345", "mime_type": "x/y", **extra}, "timestamp": "1790000000"}))
            html = c.get(f"/inbox/chat/{n}").text
            self.assertIn('src="/api/inbox/media/MIDimage12345"', html)
            self.assertIn("<audio", html); self.assertIn("/api/inbox/media/MIDaudio12345", html)
            self.assertIn('href="/api/inbox/media/MIDdocument12345"', html); self.assertIn("po.pdf", html)
        finally:
            sb._LOCAL_OUTBOUND.clear(); sb.ENABLED = enabled; sb._LOCAL_OUTBOUND_FILE = store
            asyncio.run(conversation_store.reset_session("60155000444"))


if __name__ == "__main__":
    unittest.main()
