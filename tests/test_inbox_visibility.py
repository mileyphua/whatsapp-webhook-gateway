"""Every message, sent or received, of every kind must show in the inbox like a normal WhatsApp chat."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store
import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

N = "60166000333"


class DescribeInbound(unittest.TestCase):
    def d(self, msg):
        return sb.describe_inbound(msg)

    def test_text(self):
        self.assertEqual(self.d({"type": "text", "text": {"body": "hello"}}), {"text": "hello", "media_type": None, "media_id": None, "filename": None, "mime": None})

    def test_image_with_caption_and_document_with_filename(self):
        r = self.d({"type": "image", "image": {"id": "IMG1", "mime_type": "image/jpeg", "caption": "my site"}})
        self.assertEqual((r["text"], r["media_type"], r["media_id"], r["mime"]), ("my site", "image", "IMG1", "image/jpeg"))
        r = self.d({"type": "document", "document": {"id": "DOC1", "mime_type": "application/pdf", "filename": "po.pdf"}})
        self.assertEqual((r["text"], r["media_type"], r["media_id"], r["filename"]), ("", "document", "DOC1", "po.pdf"))

    def test_voice_note_video_and_sticker(self):
        self.assertEqual(self.d({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus"}})["media_type"], "audio")
        self.assertEqual(self.d({"type": "video", "video": {"id": "V1", "mime_type": "video/mp4"}})["media_type"], "video")
        r = self.d({"type": "sticker", "sticker": {"id": "S1", "mime_type": "image/webp"}})
        self.assertEqual((r["media_type"], r["media_id"]), ("image", "S1"))

    def test_button_and_list_replies_show_what_the_buyer_picked(self):
        self.assertEqual(self.d({"type": "button", "button": {"text": "Yes, send quote"}})["text"], "Yes, send quote")
        self.assertEqual(self.d({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"title": "Call me"}}})["text"], "Call me")
        self.assertEqual(self.d({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"title": "Bitumen 60/70"}}})["text"], "Bitumen 60/70")

    def test_location_contacts_reaction_and_unknown(self):
        self.assertIn("Port Klang", self.d({"type": "location", "location": {"latitude": 3.0, "longitude": 101.4, "name": "Port Klang"}})["text"])
        self.assertIn("Ali Hassan", self.d({"type": "contacts", "contacts": [{"name": {"formatted_name": "Ali Hassan"}}]})["text"])
        self.assertEqual(self.d({"type": "reaction", "reaction": {"emoji": "👍", "message_id": "w"}})["text"], "👍")
        self.assertIn("not supported", self.d({"type": "order", "order": {}})["text"].lower())


class Base(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        self._enabled = sb.ENABLED; sb.ENABLED = False
        self._store = sb._LOCAL_OUTBOUND_FILE; sb._LOCAL_OUTBOUND_FILE = tempfile.mktemp(suffix=".json")
        sb._LOCAL_OUTBOUND.clear()
        main._LOGIN_FAILS.clear()
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        sb.ENABLED = self._enabled
        if os.path.exists(sb._LOCAL_OUTBOUND_FILE): os.remove(sb._LOCAL_OUTBOUND_FILE)
        sb._LOCAL_OUTBOUND_FILE = self._store
        sb._LOCAL_OUTBOUND.clear()
        asyncio.run(conversation_store.reset_session(N))
        if os.path.exists(self._file): os.remove(self._file)

    def thread(self):
        r = self.c.get(f"/api/inbox/chats/{N}/messages").json()
        return r["messages"] if isinstance(r, dict) else r

    def inbound(self, text, wamid="wamid.IN1", **extra):
        asyncio.run(self.main._persist_inbound_safe({"id": wamid, "from": N, "type": "text", "text": {"body": text}, "timestamp": "1790000000", **extra}))

    def outbound(self, text, direction="ai", **kw):
        asyncio.run(self.main._persist_outbound_safe(e164=N, direction=direction, text=text, sent_id_from_graph=kw.pop("sid", None), **kw))


class LocalConversationIsComplete(Base):
    def test_buyer_ai_and_human_messages_all_show_once_in_order(self):
        self.inbound("Do you sell bitumen?")
        s = asyncio.run(conversation_store.get_session(N)); s.append("user", "Do you sell bitumen?")   # what the AI layer also records
        self.outbound("Yes we do. How many tonnes?", "ai", sid="wamid.AI1")
        s.append("assistant", "Yes we do. How many tonnes?")
        self.inbound("500 tonnes to Port Klang", wamid="wamid.IN2")
        self.outbound("I'll call you to confirm.", "human", sent_by="Mei Ling")
        got = [(m["direction"], m["text"]) for m in self.thread()]
        self.assertEqual(got, [("buyer", "Do you sell bitumen?"), ("ai", "Yes we do. How many tonnes?"),
                               ("buyer", "500 tonnes to Port Klang"), ("human", "I'll call you to confirm.")])

    def test_chat_history_from_before_this_feature_is_kept(self):
        s = asyncio.run(conversation_store.get_session(N)); s.append("user", "older question"); s.append("assistant", "older answer")
        self.inbound("new question", wamid="wamid.IN3")
        got = [m["text"] for m in self.thread()]
        self.assertEqual(got, ["older question", "older answer", "new question"])

    def test_the_list_shows_the_latest_message_whoever_sent_it(self):
        self.inbound("hello there")
        self.outbound("Hi, how can I help?", "human", sent_by="Mei Ling")
        row = [c for c in self.c.get("/api/inbox/chats").json()["chats"] if c["e164"] == N][0]
        self.assertEqual(row["last_message_text"], "Hi, how can I help?")

    def test_a_received_photo_and_voice_note_are_listed_with_media_ids(self):
        asyncio.run(self.main._persist_inbound_safe({"id": "wamid.P1", "from": N, "type": "image", "image": {"id": "IMG9", "mime_type": "image/jpeg", "caption": "site"}, "timestamp": "1790000001"}))
        asyncio.run(self.main._persist_inbound_safe({"id": "wamid.P2", "from": N, "type": "audio", "audio": {"id": "AUD9", "mime_type": "audio/ogg"}, "timestamp": "1790000002"}))
        m = self.thread()
        self.assertEqual([(x["media_type"], x["media_id"], x["text"]) for x in m], [("image", "IMG9", "site"), ("audio", "AUD9", "")])

    def test_a_sent_file_shows_the_file_not_a_filler_line(self):
        self.outbound("📎 specs.pdf", "human", media_type="document", media_meta={"filename": "specs.pdf", "media_id": "M9"})
        m = self.thread()[0]
        self.assertEqual((m["text"], m["media_id"], m["filename"]), ("", "M9", "specs.pdf"))

    def test_delivery_status_is_recorded_on_the_sent_message(self):
        self.outbound("hello", "human", sid="wamid.S1")
        asyncio.run(sb.mark_status("wamid.S1", N, "delivered"))
        asyncio.run(sb.mark_status("wamid.S1", N, "read"))
        st = self.thread()[0]["meta_statuses_jsonb"]
        self.assertIn("read", st); self.assertIn("delivered", st)

    def test_failed_delivery_marks_the_message_errored(self):
        self.outbound("hello", "human", sid="wamid.S2")
        asyncio.run(sb.mark_status("wamid.S2", N, "failed", "#131047 Re-engagement message"))
        m = self.thread()[0]
        self.assertTrue(m["errored"]); self.assertIn("131047", m["error_detail"])


if __name__ == "__main__":
    unittest.main()


class FakeDb:
    posted = []
    rows = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, headers=None, json=None, **k):
        FakeDb.posted.append(json)
        class R: status_code = 201
        return R()

    async def get(self, url, headers=None, params=None, **k):
        class R:
            status_code = 200
            def json(s): return list(reversed(FakeDb.rows))
        return R()


class ListPreviewForMedia(unittest.TestCase):
    def setUp(self):
        FakeDb.posted = []; FakeDb.rows = []
        self._p = [mock.patch.object(sb, "ENABLED", True), mock.patch.object(sb, "_client", lambda: FakeDb())]
        for p in self._p: p.start()

    def tearDown(self):
        for p in self._p: p.stop()

    def test_a_photo_without_caption_gets_a_readable_list_preview(self):
        asyncio.run(sb.insert_inbound_message({"id": "w1", "from": N, "type": "image", "image": {"id": "IMG1", "mime_type": "image/jpeg"}, "timestamp": "1790000000"}))
        asyncio.run(sb.insert_inbound_message({"id": "w2", "from": N, "type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg"}}))
        asyncio.run(sb.insert_inbound_message({"id": "w3", "from": N, "type": "document", "document": {"id": "D1", "filename": "po.pdf"}}))
        asyncio.run(sb.insert_inbound_message({"id": "w4", "from": N, "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"title": "Call me"}}}))
        self.assertEqual([p["text"] for p in FakeDb.posted], ["📷 Photo", "🎤 Voice message", "📄 po.pdf", "Call me"])
        self.assertEqual([p["media_type"] for p in FakeDb.posted], ["image", "audio", "document", None])
        self.assertEqual(FakeDb.posted[0]["media_url"], "IMG1")

    def test_the_placeholder_is_hidden_in_the_bubble_but_a_real_caption_is_kept(self):
        FakeDb.rows = [
            {"id": 1, "direction": "buyer", "text": "📷 Photo", "media_type": "image", "media_url": "IMG1", "payload_jsonb": {"image": {"id": "IMG1", "mime_type": "image/jpeg"}}},
            {"id": 2, "direction": "buyer", "text": "my site", "media_type": "image", "media_url": "IMG2", "payload_jsonb": {}},
            {"id": 3, "direction": "buyer", "text": "📄 po.pdf", "media_type": "document", "media_url": "D1", "payload_jsonb": {"document": {"id": "D1", "filename": "po.pdf"}}},
            {"id": 4, "direction": "human", "text": "📎 specs.pdf", "media_type": "document", "media_url": None, "payload_jsonb": {"filename": "specs.pdf", "media_id": "M9", "sent_by": "Mei"}},
        ]
        got = asyncio.run(sb.thread_messages(N))
        self.assertEqual([(m["text"], m["media_id"], m["filename"]) for m in got],
                         [("", "IMG1", None), ("my site", "IMG2", None), ("", "D1", "po.pdf"), ("", "M9", "specs.pdf")])


class WebhookToInbox(Base):
    """The real path: Meta posts to /webhook -> the AI answers -> everything shows in the inbox, once, in order."""

    def test_a_buyer_text_a_photo_and_the_ai_reply_all_appear(self):
        import time as _t
        main = self.main

        class FakeMeta:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None, **k):
                class R:
                    status_code = 200; content = b"{}"; text = "{}"
                    def json(s): return {"messages": [{"id": "wamid.AIREPLY1"}]}
                return R()

        async def handle(**kw):
            s = await conversation_store.get_session(N)
            s.append("user", kw.get("inbound_text") or ""); s.append("assistant", "Yes, we supply bitumen 60/70.")
            return "Yes, we supply bitumen 60/70."

        async def not_held(_n, _s=None): return None

        def payload(msg):
            return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "PN1"}, "contacts": [{"profile": {"name": "Ali"}, "wa_id": N}], "messages": [msg]}}]}]}

        with mock.patch.object(main, "WHATSAPP_APP_SECRET", ""), mock.patch.object(main, "ACCESS_TOKEN", "tok"), \
                mock.patch.object(main, "PHONE_NUMBER_ID", "PN1"), mock.patch.object(main.httpx, "AsyncClient", FakeMeta), \
                mock.patch.object(main.llm_assistant, "handle_incoming_message", handle), mock.patch.object(main, "_claim_is_held_by_other", not_held), \
                mock.patch.object(main, "DEBOUNCE_SECONDS", 0.05), TestClient(main.app) as c:
            c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
            c.post("/webhook", json=payload({"id": "wamid.B1", "from": N, "type": "text", "timestamp": str(int(_t.time())), "text": {"body": "Do you sell bitumen?"}}))
            c.post("/webhook", json=payload({"id": "wamid.B2", "from": N, "type": "image", "timestamp": str(int(_t.time())), "image": {"id": "IMGWEB123", "mime_type": "image/jpeg", "caption": "our site"}}))
            for _ in range(60):
                got = c.get(f"/api/inbox/chats/{N}/messages").json()["messages"]
                if any(m["text"] == "Yes, we supply bitumen 60/70." for m in got):
                    break
                _t.sleep(0.1)
        got = [(m["direction"], m["text"], m.get("media_id")) for m in got]
        self.assertIn(("buyer", "Do you sell bitumen?", None), got)
        self.assertIn(("buyer", "our site", "IMGWEB123"), got)
        self.assertEqual(len([g for g in got if g[1] == "Do you sell bitumen?"]), 1)      # the buyer's text appears once, not twice
        self.assertEqual(len([g for g in got if g[1] == "Yes, we supply bitumen 60/70."]), 1)
        texts = [g[1] for g in got]
        self.assertLess(texts.index("Do you sell bitumen?"), texts.index("Yes, we supply bitumen 60/70."))
