"""When a buyer sends something the AI cannot read (voice note, video, an old Word/PowerPoint file, or a file that fails to open),
the AI says nothing: it notifies a person in the inbox to take over and stays silent for the whole chat until a person does."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store as cs
import feedback_store as fs
import media_ai
import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

N = "60166000321"


def run(c):
    return asyncio.run(c)


class Base(unittest.TestCase):
    _seq = [0]

    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_pause_fs.json"
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name); us._FILE = self._u = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24; main._LOGIN_FAILS.clear(); main._MEDIA_EMAIL_LAST.clear(); main._PAUSE_NOTE_LAST.clear()
        self._en = sb.ENABLED; sb.ENABLED = False
        self._st = sb._LOCAL_OUTBOUND_FILE; sb._LOCAL_OUTBOUND_FILE = "/tmp/_pause_out.json"; sb._LOCAL_OUTBOUND.clear()
        self.sent, self.emails, self.llm_inputs = [], [], []

        async def send_http(*a, **k):                 # the WhatsApp API: any real send lands here
            payload = k.get("json") or {}
            self.sent.append(payload)
            class R:
                status_code = 200; content = b"{}"; text = "{}"
                def json(s): return {"messages": [{"id": "wamid.X%d" % len(self.sent)}]}
            return R()

        class FakeHttp:
            def __init__(s, *a, **k): pass
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
            post = send_http

        async def email(**kw): self.emails.append(kw); return True
        async def handle(**kw):
            self.llm_inputs.append(kw.get("inbound_text")); return "AI says hello."
        async def not_held(_n, _s=None): return None

        self.reading = media_ai.MediaReading(True, "A spec sheet for 60/70.", "")
        async def understand(**kw): return self.reading

        self._p = [mock.patch.object(main, "WHATSAPP_APP_SECRET", ""), mock.patch.object(main, "ACCESS_TOKEN", "tok"), mock.patch.object(main, "PHONE_NUMBER_ID", "PN1"),
                   mock.patch.object(main.httpx, "AsyncClient", FakeHttp), mock.patch.object(main.notify, "send_handoff_email", email),
                   mock.patch.object(main.llm_assistant, "handle_incoming_message", handle), mock.patch.object(main, "_claim_is_held_by_other", not_held),
                   mock.patch.object(main.media_ai, "understand", understand), mock.patch.object(main, "DEBOUNCE_SECONDS", 0.05)]
        for p in self._p: p.start()
        asyncio.run(cs.reset_session(N))

    def tearDown(self):
        for p in self._p: p.stop()
        sb.ENABLED = self._en; sb._LOCAL_OUTBOUND.clear(); sb._LOCAL_OUTBOUND_FILE = self._st
        asyncio.run(cs.reset_session(N))
        if os.path.exists(self._u): os.remove(self._u)

    def post(self, msg, wait=0.5):
        Base._seq[0] += 1
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"}, "messages": [{"id": f"wamid.P{os.getpid()}-{Base._seq[0]}", "from": N, "timestamp": str(int(time.time())), **msg}]}}]}]}
        with TestClient(self.main.app) as c:
            c.post("/webhook", json=payload)
            time.sleep(wait)

    def notes(self):
        return [t for d, t in ((m["direction"], m["text"]) for m in sb.local_outbound_messages(N)) if d == "system"]

    def sess(self):
        return cs._SESSIONS.get(N)

    def texts_to_buyer(self):
        return [p for p in self.sent if p.get("type") == "text"]


class WhatTheAiCannotRead(Base):
    def test_a_voice_note_and_a_video_get_no_reply_and_a_person_is_asked_to_take_over(self):
        for msg, word in (({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg"}}, "voice note"),
                          ({"type": "video", "video": {"id": "V1", "mime_type": "video/mp4"}}, "video")):
            asyncio.run(cs.reset_session(N)); self.sent.clear(); sb._LOCAL_OUTBOUND.clear()
            self.post(msg)
            self.assertEqual(self.texts_to_buyer(), [], word)                                   # nothing is sent to the buyer
            self.assertTrue(any(word in n.lower() and "take over" in n.lower() for n in self.notes()), (word, self.notes()))
            s = self.sess(); self.assertTrue(s.ai_paused); self.assertIsNotNone(s.needs_human_since)
            self.assertIn(word, s.needs_human_reason.lower()); self.assertIn("ai paused", s.needs_human_reason.lower())

    def test_old_word_and_powerpoint_files_are_handed_to_a_person_too(self):
        self.reading = media_ai.MediaReading(False, "", "unsupported_type")
        for name, mime in (("offer.doc", "application/msword"), ("deck.ppt", "application/vnd.ms-powerpoint"),
                           ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")):
            asyncio.run(cs.reset_session(N)); self.sent.clear(); sb._LOCAL_OUTBOUND.clear()
            self.post({"type": "document", "document": {"id": "D1", "mime_type": mime, "filename": name}})
            self.assertEqual(self.texts_to_buyer(), [], name)
            self.assertTrue(any(name in n and "take over" in n.lower() for n in self.notes()), (name, self.notes()))
            self.assertTrue(self.sess().ai_paused, name)

    def test_a_file_that_fails_to_open_is_handled_the_same_way(self):
        for reason in ("download_failed", "model_error", "too_large", "model_cannot_read", "empty"):
            asyncio.run(cs.reset_session(N)); self.sent.clear(); sb._LOCAL_OUTBOUND.clear()
            self.reading = media_ai.MediaReading(False, "", reason)
            self.post({"type": "image", "image": {"id": "I1", "mime_type": "image/jpeg"}})
            self.assertEqual(self.texts_to_buyer(), [], reason); self.assertTrue(self.sess().ai_paused, reason)

    def test_the_team_is_emailed_once_an_hour_and_the_chat_shows_in_needs_human(self):
        self.post({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg"}}); self.post({"type": "video", "video": {"id": "V1", "mime_type": "video/mp4"}})
        self.assertEqual(len(self.emails), 1)
        s = self.sess(); self.assertIsNotNone(s.needs_human_since)
        c = TestClient(self.main.app); c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        body = c.get("/api/inbox/handoffs").json()
        items = body if isinstance(body, list) else next(v for v in body.values() if isinstance(v, list))
        self.assertTrue(any(i["e164"] == N and "AI paused" in i["reason"] for i in items), items)

    def test_a_readable_photo_is_still_read(self):
        self.post({"type": "image", "image": {"id": "I2", "mime_type": "image/jpeg", "caption": "our order"}})
        self.assertFalse(getattr(self.sess(), "ai_paused", False)); self.assertTrue(any("AI read the image" in n for n in self.notes()))

    def test_stickers_reactions_contacts_and_locations_keep_their_old_behaviour(self):
        self.post({"type": "contacts", "contacts": [{"name": {"formatted_name": "Ali"}}]})
        self.assertEqual(len(self.texts_to_buyer()), 1); self.assertFalse(getattr(self.sess(), "ai_paused", False))


class SilentUntilAPersonTakesOver(Base):
    def pause(self):
        self.post({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg"}})
        self.sent.clear()

    def test_later_messages_from_the_buyer_get_no_ai_reply(self):
        self.pause()
        self.post({"type": "text", "text": {"body": "hello? did you get my voice note?"}}, wait=0.8)
        self.assertEqual(self.llm_inputs, []); self.assertEqual(self.texts_to_buyer(), [])
        self.assertTrue(any("paused" in n.lower() and "did you get my voice note" in n for n in self.notes()), self.notes())

    def test_an_instant_human_request_also_gets_no_ai_message(self):
        self.pause()
        self.post({"type": "text", "text": {"body": "I want to speak to a human"}}, wait=0.5)
        self.assertEqual(self.texts_to_buyer(), [])

    def test_a_readable_photo_while_paused_is_noted_for_the_person_but_not_answered(self):
        self.pause()
        self.post({"type": "image", "image": {"id": "I3", "mime_type": "image/jpeg"}}, wait=0.8)
        self.assertEqual(self.llm_inputs, []); self.assertEqual(self.texts_to_buyer(), [])
        self.assertTrue(any("AI read the image" in n for n in self.notes()))

    def test_no_ai_message_can_leave_a_paused_chat_by_any_route(self):
        self.pause()
        out = run(self.main.send_whatsapp_text(to=N, text="Hi", sender_direction="ai"))
        self.assertEqual(out.get("skipped"), "ai_paused"); self.assertEqual(self.sent, [])
        run(self.main.send_whatsapp_text(to=N, text="Hi from a person", sender_direction="human", sent_by="Mei"))
        self.assertEqual(len(self.texts_to_buyer()), 1)                                           # people can always write
        self.assertEqual(cs.scan_for_followups(), [])                                             # no scheduled nudge or reminder either

    def test_when_a_person_replies_the_ai_is_back(self):
        self.pause()
        s = self.sess(); s.last_buyer_ts = time.time()
        c = TestClient(self.main.app); c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        r = c.post(f"/api/inbox/chats/{N}/messages", json={"text": "Hi, this is Mei, I will listen to your voice note now."})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.sess(); self.assertFalse(s.ai_paused); self.assertIsNone(s.needs_human_since)
        self.post({"type": "text", "text": {"body": "thanks, what is 60/70?"}}, wait=0.8)
        self.assertEqual(self.llm_inputs, ["thanks, what is 60/70?"])

    def test_the_ai_suggest_draft_still_works_for_the_person(self):
        self.pause()
        c = TestClient(self.main.app); c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.assertEqual(c.get(f"/api/inbox/chats/{N}/claim").json().get("ai_paused"), True)     # the thread page can show "AI paused"


if __name__ == "__main__":
    unittest.main()
