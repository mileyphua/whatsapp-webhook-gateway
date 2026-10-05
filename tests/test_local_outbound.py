"""Without Supabase (local development) a message a person sends from the inbox must still show in the thread and the
chat list; before, only what the AI remembered in Redis was shown, so sent templates and human replies vanished."""
import asyncio
import os
import tempfile
import unittest

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import conversation_store
import supabase_client as sb
import users_store as us
from fastapi.testclient import TestClient

N = "60188000222"


class LocalOutbound(unittest.TestCase):
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

    def send(self, text="Hello Ahmed, specs attached.", **kw):
        asyncio.run(self.main._persist_outbound_safe(e164=N, direction="human", text=text, sent_id_from_graph="wamid.L1", sent_by="Mei Ling", **kw))

    def thread(self):
        return self.c.get(f"/api/inbox/chats/{N}/messages").json()

    def test_a_sent_message_shows_in_the_thread_with_who_sent_it(self):
        self.send()
        msgs = self.thread(); msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        self.assertEqual([(m["direction"], m["text"], m.get("held_by")) for m in msgs], [("human", "Hello Ahmed, specs attached.", "Mei Ling")])

    def test_a_chat_that_only_has_sent_messages_is_in_the_list_and_the_page(self):
        self.send()
        listed = self.c.get("/api/inbox/chats").json(); listed = listed["chats"] if isinstance(listed, dict) else listed
        self.assertIn(N, [c["e164"] for c in listed])
        self.assertIn(N, self.c.get("/inbox/chats").text)

    def test_sent_file_keeps_its_name_and_type(self):
        self.send("📎 specs.pdf", media_type="document", media_meta={"filename": "specs.pdf"})
        msgs = self.thread(); msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        self.assertEqual((msgs[0]["media_type"], msgs[0]["filename"]), ("document", "specs.pdf"))

    def test_messages_survive_a_server_restart(self):
        self.send()
        sb._LOCAL_OUTBOUND.clear(); sb._load_local_outbound()
        msgs = self.thread(); msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        self.assertEqual(len(msgs), 1)

    def test_ai_history_and_sent_messages_are_shown_together(self):
        s = asyncio.run(conversation_store.get_session(N)); s.append("user", "Do you sell bitumen?"); s.append("assistant", "Yes we do.")
        self.send("Following up by phone.")
        msgs = self.thread(); msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        self.assertEqual(sorted(m["text"] for m in msgs), ["Do you sell bitumen?", "Following up by phone.", "Yes we do."])

    def test_deleting_the_chat_removes_the_sent_messages_too(self):
        self.send()
        self.assertEqual(self.c.delete(f"/api/inbox/chats/{N}").status_code, 200)
        listed = self.c.get("/api/inbox/chats").json(); listed = listed["chats"] if isinstance(listed, dict) else listed
        self.assertNotIn(N, [c["e164"] for c in listed])
        msgs = self.thread(); msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()
