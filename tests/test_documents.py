"""PDF library (RED first): people add PDFs, the AI reads each one once (summary + when to send it), and during a chat
the AI decides whether to attach it or use what it says. No network: the model is faked."""
import asyncio
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""
os.environ.setdefault("OPENROUTER_API_KEY", "test")

import conversation_store as cs
import documents as D
import feedback_store as fs
import llm_assistant as L

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
DIGEST = {"title": "Bitumen 60/70 Technical Datasheet", "summary": "Typical properties and test methods for Bitumen 60/70.",
          "send_when": "When the buyer asks for the datasheet, full specs or a technical document for 60/70.",
          "topics": ["60/70", "datasheet", "penetration", "specifications"],
          "facts": "Penetration at 25C: 60-70 dmm. Softening point: 46-56 C. Flash point: min 250 C."}


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="docs_test_")
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = os.path.join(self._tmp, "store.json")
        D._DIR = os.path.join(self._tmp, "files")
        D._cache_clear() if hasattr(D, "_cache_clear") else None

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def add_ready(self, **over):
        doc = run(D.add(filename="datasheet-60-70.pdf", data=PDF, uploaded_by="Mei Ling"))
        with mock.patch.object(D, "_digest", mock.AsyncMock(return_value=dict(DIGEST))):
            run(D.process(doc["id"]))
        if over:
            run(D.update(doc["id"], **over))
        return run(D.get(doc["id"]))


class Storing(Base):
    def test_adding_a_pdf_keeps_it_and_marks_it_as_being_read(self):
        doc = run(D.add(filename="brochure.pdf", data=PDF, uploaded_by="Mei Ling"))
        self.assertEqual(doc["status"], "processing")
        self.assertEqual(doc["filename"], "brochure.pdf")
        self.assertEqual(doc["uploaded_by"], "Mei Ling")
        self.assertEqual(doc["size"], len(PDF))
        self.assertEqual(run(D.read_bytes(doc["id"])), PDF)

    def test_only_real_pdfs_are_accepted(self):
        for name, data in [("notes.txt", b"hello"), ("fake.pdf", b"not a pdf at all"), ("empty.pdf", b"")]:
            with self.assertRaises(D.DocumentRejected, msg=name):
                run(D.add(filename=name, data=data, uploaded_by="x"))

    def test_too_big_is_refused(self):
        with self.assertRaises(D.DocumentRejected):
            run(D.add(filename="big.pdf", data=b"%PDF-" + b"0" * D.MAX_BYTES, uploaded_by="x"))

    def test_unsafe_file_names_are_cleaned(self):
        doc = run(D.add(filename="../../etc/passwd<script>.pdf", data=PDF, uploaded_by="x"))
        self.assertNotIn("/", doc["filename"])
        self.assertNotIn("<", doc["filename"])
        self.assertTrue(doc["filename"].endswith(".pdf"))

    def test_list_get_update_delete(self):
        d = self.add_ready()
        self.assertEqual([x["id"] for x in run(D.list_docs())], [d["id"]])
        run(D.update(d["id"], title="My title", summary="Mine", send_when="Whenever asked", enabled=False))
        got = run(D.get(d["id"]))
        self.assertEqual((got["title"], got["summary"], got["send_when"], got["enabled"]), ("My title", "Mine", "Whenever asked", False))
        run(D.delete(d["id"]))
        self.assertIsNone(run(D.get(d["id"])))
        self.assertEqual(run(D.list_docs()), [])
        self.assertIsNone(run(D.read_bytes(d["id"])))

    def test_update_ignores_fields_people_may_not_edit(self):
        d = self.add_ready()
        run(D.update(d["id"], status="ready", size=1, uploaded_by="hacker", facts="x"))
        got = run(D.get(d["id"]))
        self.assertEqual(got["uploaded_by"], "Mei Ling")
        self.assertEqual(got["size"], len(PDF))

    def test_big_files_are_kept_in_pieces_when_redis_is_used(self):
        store = {}

        async def hset(name, key, value):
            store.setdefault(name, {})[key] = json.loads(json.dumps(value))

        async def hgetall(name):
            return dict(store.get(name, {}))

        async def hdel(name, key):
            store.get(name, {}).pop(key, None)

        big = b"%PDF-" + os.urandom(D.CHUNK_BYTES * 2 + 123)
        with mock.patch.object(fs, "_redis_on", lambda: True), mock.patch.object(fs, "_hset", hset), \
                mock.patch.object(fs, "_hgetall", hgetall), mock.patch.object(fs, "_hdel", hdel):
            doc = run(D.add(filename="big.pdf", data=big, uploaded_by="x"))
            file_hashes = [k for k in store if k.startswith("doc_file_")]
            self.assertEqual(len(file_hashes), 1)
            self.assertEqual(len(store[file_hashes[0]]), 3)        # three pieces
            self.assertEqual(run(D.read_bytes(doc["id"])), big)
            run(D.delete(doc["id"]))
            self.assertEqual(store.get(file_hashes[0]), {})


class Reading(Base):
    def test_the_ai_reading_fills_title_summary_when_to_send_and_facts(self):
        d = self.add_ready()
        self.assertEqual(d["status"], "ready")
        self.assertEqual(d["title"], DIGEST["title"])
        self.assertIn("datasheet", d["send_when"].lower())
        self.assertIn("46-56", d["facts"])
        self.assertTrue(d["enabled"])

    def test_a_failed_reading_says_why_and_is_never_used(self):
        doc = run(D.add(filename="scan.pdf", data=PDF, uploaded_by="x"))
        with mock.patch.object(D, "_digest", mock.AsyncMock(side_effect=D.DigestFailed("The AI model cannot read PDFs."))):
            run(D.process(doc["id"]))
        got = run(D.get(doc["id"]))
        self.assertEqual(got["status"], "failed")
        self.assertIn("cannot read", got["error"])
        self.assertEqual(run(D.enabled_docs()), [])

    def test_reading_again_keeps_what_a_person_edited(self):
        d = self.add_ready(send_when="Only if the buyer asks twice")
        with mock.patch.object(D, "_digest", mock.AsyncMock(return_value=dict(DIGEST))):
            run(D.process(d["id"], keep_edits=True))
        self.assertEqual(run(D.get(d["id"]))["send_when"], "Only if the buyer asks twice")

    def test_digest_json_is_parsed_even_with_chatter_around_it(self):
        raw = 'Sure! Here you go:\n```json\n' + json.dumps(DIGEST) + '\n```'
        out = D.parse_digest(raw)
        self.assertEqual(out["title"], DIGEST["title"])
        self.assertEqual(out["topics"], DIGEST["topics"])

    def test_digest_without_a_summary_is_rejected(self):
        with self.assertRaises(D.DigestFailed):
            D.parse_digest('{"title": "x"}')
        with self.assertRaises(D.DigestFailed):
            D.parse_digest("I could not read it")

    def test_digest_values_are_trimmed(self):
        out = D.parse_digest(json.dumps({**DIGEST, "facts": "f" * 50000, "summary": "s" * 5000}))
        self.assertLessEqual(len(out["facts"]), D.MAX_FACTS)
        self.assertLessEqual(len(out["summary"]), 600)


class Choosing(Base):
    def test_only_ready_enabled_docs_are_offered_to_the_ai(self):
        a = self.add_ready()
        b = run(D.add(filename="b.pdf", data=PDF, uploaded_by="x"))          # still being read
        c = self.add_ready(enabled=False)
        ids = [x["id"] for x in run(D.enabled_docs())]
        self.assertEqual(ids, [a["id"]])
        self.assertNotIn(b["id"], ids)
        self.assertNotIn(c["id"], ids)

    def test_the_prompt_block_tells_the_ai_when_to_send_each_document(self):
        d = self.add_ready()
        block = D.prompt_block([d], sent_ids=[])
        self.assertIn(d["id"], block)
        self.assertIn(DIGEST["send_when"], block)
        self.assertIn("send_document", block)
        self.assertIn("never", block.lower())                                # rules, not just a list

    def test_a_document_already_sent_is_marked_so(self):
        d = self.add_ready()
        self.assertIn("already sent", D.prompt_block([d], sent_ids=[d["id"]]).lower())

    def test_facts_are_offered_only_for_questions_the_document_covers(self):
        d = self.add_ready()
        hit = D.relevant_chunks("what is the softening point of 60/70?", [d])
        self.assertEqual(len(hit), 1)
        self.assertIn("46-56", hit[0]["chunk_text"])
        self.assertEqual(D.relevant_chunks("do you ship to Vietnam?", [d]), [])
        self.assertEqual(D.relevant_chunks("hi", [d]), [])

    def test_at_most_two_documents_feed_one_answer(self):
        docs = []
        for i in range(4):
            docs.append({**DIGEST, "id": f"d{i}", "title": f"Sheet {i}", "status": "ready", "enabled": True})
        self.assertLessEqual(len(D.relevant_chunks("60/70 datasheet please", docs)), 2)

    def test_marking_sent_counts_it(self):
        d = self.add_ready()
        run(D.mark_sent(d["id"]))
        run(D.mark_sent(d["id"]))
        self.assertEqual(run(D.get(d["id"]))["times_sent"], 2)


def _tool_then_text(doc_id, text, caption="Here is the datasheet."):
    rounds = {"n": 0}
    seen = {"tools": [], "system": []}

    async def create(**kw):
        seen["tools"].append([t["function"]["name"] for t in (kw.get("tools") or [])])
        seen["system"].append(" ".join(m["content"] for m in kw["messages"] if m["role"] == "system" and isinstance(m.get("content"), str)))
        rounds["n"] += 1
        if rounds["n"] == 1 and doc_id:
            call = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="send_document", arguments=json.dumps({"document_id": doc_id, "caption": caption})))
            msg = types.SimpleNamespace(content=None, tool_calls=[call])
        else:
            msg = types.SimpleNamespace(content=text, tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))), seen


class TheAiDecides(Base):
    def setUp(self):
        super().setUp()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""
        cs._SESSIONS.clear()

    def session(self, phone="60177700001"):
        s = cs.ConversationSession(phone_number=phone)
        s.append("user", "can you send me the datasheet for 60/70?")
        return s

    def test_the_send_document_tool_exists_only_when_there_are_documents(self):
        client, seen = _tool_then_text(None, "Sure.")
        with mock.patch.object(L, "_openrouter_client", return_value=client):
            run(L._single_turn_chat(session=self.session(), references=[]))
        self.assertNotIn("send_document", seen["tools"][0])
        self.add_ready()
        client, seen = _tool_then_text(None, "Sure.")
        with mock.patch.object(L, "_openrouter_client", return_value=client):
            run(L._single_turn_chat(session=self.session(), references=[]))
        self.assertIn("send_document", seen["tools"][0])
        self.assertIn(DIGEST["send_when"], seen["system"][0])

    def test_the_ai_sending_a_document_queues_it_once(self):
        d = self.add_ready()
        s = self.session()
        client, _ = _tool_then_text(d["id"], "I've attached the 60/70 datasheet for you.")
        with mock.patch.object(L, "_openrouter_client", return_value=client):
            text, tools = run(L._single_turn_chat(session=s, references=[]))
        self.assertIn("send_document", tools)
        self.assertEqual(s.docs_to_send, [d["id"]])
        self.assertIn("attached", text)

    def test_it_is_not_queued_twice_in_one_chat(self):
        d = self.add_ready()
        s = self.session()
        s.docs_sent = [d["id"]]
        out = run(L._run_tool("send_document", {"document_id": d["id"]}, session=s))
        self.assertEqual(s.docs_to_send, [])
        self.assertIn("already", out.lower())

    def test_an_unknown_or_switched_off_document_is_refused(self):
        d = self.add_ready(enabled=False)
        s = self.session()
        for doc_id in (d["id"], "nope"):
            out = run(L._run_tool("send_document", {"document_id": doc_id}, session=s))
            self.assertEqual(s.docs_to_send, [])
            self.assertIn("not available", out.lower())

    def test_one_reply_attaches_at_most_one_document(self):
        a = self.add_ready()
        b = self.add_ready()
        s = self.session()
        run(L._run_tool("send_document", {"document_id": a["id"]}, session=s))
        out = run(L._run_tool("send_document", {"document_id": b["id"]}, session=s))
        self.assertEqual(s.docs_to_send, [a["id"]])
        self.assertIn("one document", out.lower())

    def test_a_document_that_covers_the_question_stops_the_no_match_handoff(self):
        d = self.add_ready()
        client, seen = _tool_then_text(None, "Penetration is 60-70 dmm and the softening point is 46-56 C.")

        async def no_refs(_q, **kw):
            return []

        async def no_plan(**kw):
            return None

        async def no_skills():
            return []

        import learning
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", no_refs), \
                mock.patch.object(L, "index_is_ready", lambda: True), mock.patch.object(learning, "plan_reply", no_plan), \
                mock.patch.object(learning, "active_skills", no_skills):
            reply = run(L.handle_incoming_message(phone_number="60177700002", inbound_text="what is the softening point of 60/70?"))
        self.assertIn("46-56", reply)                                    # went to the model, not the canned handoff
        self.assertIn("46-56", seen["system"][-1])                       # the document's facts were in front of the model
        self.assertIsNone(cs._SESSIONS["60177700002"].needs_human_since)

    def test_a_draft_reports_which_document_it_would_send_without_sending(self):
        d = self.add_ready()
        client, _ = _tool_then_text(d["id"], "Attached.")

        async def no_refs(_q, **kw):
            return []

        async def no_plan(**kw):
            return None

        async def no_skills():
            return []

        import learning
        trace = {}
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", no_refs), \
                mock.patch.object(L, "index_is_ready", lambda: True), mock.patch.object(learning, "plan_reply", no_plan), \
                mock.patch.object(learning, "active_skills", no_skills):
            run(L.handle_incoming_message(phone_number="60177700003", inbound_text="send me the 60/70 datasheet", draft_only=True, trace=trace))
        self.assertEqual(trace["documents"], [d["id"]])
        self.assertEqual(run(D.get(d["id"]))["times_sent"], 0)


class SendingTheQueue(Base):
    def setUp(self):
        super().setUp()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""
        cs._SESSIONS.clear()
        import main
        self.main = main

    def test_a_queued_document_is_sent_as_the_ai_and_remembered(self):
        d = self.add_ready()
        s = cs.ConversationSession(phone_number="60177700010")
        s.docs_to_send = [d["id"]]
        cs._SESSIONS[s.phone_number] = s
        send = mock.AsyncMock(return_value={"messages": [{"id": "wamid.X"}]})
        with mock.patch.object(self.main, "send_whatsapp_media", send), mock.patch.object(self.main, "_ai_is_paused", mock.AsyncMock(return_value=False)):
            run(self.main._send_queued_documents(s.phone_number, "pnid1"))
        kw = send.call_args.kwargs
        self.assertEqual((kw["to"], kw["kind"], kw["filename"], kw["sender_direction"]), ("60177700010", "document", "datasheet-60-70.pdf", "ai"))
        self.assertEqual(kw["mime"], "application/pdf")
        self.assertEqual(kw["data"], PDF)
        self.assertEqual(s.docs_to_send, [])
        self.assertEqual(s.docs_sent, [d["id"]])
        self.assertEqual(run(D.get(d["id"]))["times_sent"], 1)

    def test_nothing_is_sent_while_the_ai_is_paused(self):
        d = self.add_ready()
        s = cs.ConversationSession(phone_number="60177700011")
        s.docs_to_send = [d["id"]]
        cs._SESSIONS[s.phone_number] = s
        send = mock.AsyncMock()
        with mock.patch.object(self.main, "send_whatsapp_media", send), mock.patch.object(self.main, "_ai_is_paused", mock.AsyncMock(return_value=True)):
            run(self.main._send_queued_documents(s.phone_number, "pnid1"))
        send.assert_not_called()
        self.assertEqual(s.docs_to_send, [])                      # dropped, not kept for later

    def test_a_document_deleted_meanwhile_is_skipped_quietly(self):
        d = self.add_ready()
        s = cs.ConversationSession(phone_number="60177700012")
        s.docs_to_send = [d["id"]]
        cs._SESSIONS[s.phone_number] = s
        run(D.delete(d["id"]))
        send = mock.AsyncMock()
        with mock.patch.object(self.main, "send_whatsapp_media", send), mock.patch.object(self.main, "_ai_is_paused", mock.AsyncMock(return_value=False)):
            run(self.main._send_queued_documents(s.phone_number, "pnid1"))
        send.assert_not_called()
        self.assertEqual(s.docs_to_send, [])

    def test_a_failed_upload_to_whatsapp_does_not_crash_the_reply(self):
        d = self.add_ready()
        s = cs.ConversationSession(phone_number="60177700013")
        s.docs_to_send = [d["id"]]
        cs._SESSIONS[s.phone_number] = s
        with mock.patch.object(self.main, "send_whatsapp_media", mock.AsyncMock(side_effect=RuntimeError("boom"))), \
                mock.patch.object(self.main, "_ai_is_paused", mock.AsyncMock(return_value=False)):
            run(self.main._send_queued_documents(s.phone_number, "pnid1"))
        self.assertEqual(s.docs_to_send, [])
        self.assertEqual(s.docs_sent, [])

    def test_media_sent_by_the_ai_is_saved_as_the_ai(self):
        saved = {}

        async def persist(**kw):
            saved.update(kw)

        class R:
            status_code = 200
            content = b"{}"
            text = "{}"

            def json(self):
                return {"id": "MEDIA1", "messages": [{"id": "wamid.Y"}]}

        class C:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return R()

        self.main.ACCESS_TOKEN = "t"
        with mock.patch.object(self.main, "_persist_outbound_safe", persist), mock.patch.object(self.main.httpx, "AsyncClient", lambda **k: C()), \
                mock.patch.object(self.main, "_ai_is_paused", mock.AsyncMock(return_value=False)):
            run(self.main.send_whatsapp_media(to="601", kind="document", data=PDF, mime="application/pdf", filename="a.pdf",
                                              phone_number_id="p", sender_direction="ai"))
        self.assertEqual(saved["direction"], "ai")


class Endpoints(Base):
    def setUp(self):
        super().setUp()
        import users_store as us
        from fastapi.testclient import TestClient
        us._REDIS_URL = us._REDIS_TOKEN = ""
        self._users_file = os.path.join(self._tmp, "users.json")
        us._FILE = self._users_file
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._sb.ENABLED = False
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1"))
        run(us.create_user(username="raj", name="Raj", password="raj-password-1"))
        run(main._refresh_users_cache())
        self.mei = TestClient(main.app); self.mei.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)
        self.raj = TestClient(main.app); self.raj.post("/inbox/login", data={"username": "raj", "password": "raj-password-1"}, follow_redirects=False)
        self.admin = TestClient(main.app); self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.anon = TestClient(main.app)
        self._digest = mock.patch.object(D, "_digest", mock.AsyncMock(return_value=dict(DIGEST)))
        self._digest.start()

    def tearDown(self):
        self._digest.stop()
        self.main._USERS_CACHE.clear()
        super().tearDown()

    def upload(self, client, name="datasheet.pdf", data=PDF, ctype="application/pdf"):
        return client.post("/api/inbox/documents", files={"file": (name, data, ctype)})

    def test_everything_needs_a_login(self):
        for method, path in [("get", "/api/inbox/documents"), ("post", "/api/inbox/documents"), ("get", "/api/inbox/documents/x/file"),
                             ("put", "/api/inbox/documents/x"), ("delete", "/api/inbox/documents/x"), ("post", "/api/inbox/documents/test")]:
            self.assertEqual(getattr(self.anon, method)(path).status_code, 401, path)
        self.assertEqual(self.anon.get("/inbox/documents", follow_redirects=False).status_code, 302)

    def test_a_team_member_can_add_a_pdf_and_it_gets_read(self):
        r = self.upload(self.mei)
        self.assertEqual(r.status_code, 201)
        doc = r.json()["document"]
        self.assertEqual(doc["uploaded_by"], "Mei Ling")
        listed = self.mei.get("/api/inbox/documents").json()["documents"]
        self.assertEqual(listed[0]["status"], "ready")              # the background reading finished
        self.assertEqual(listed[0]["title"], DIGEST["title"])
        self.assertNotIn("facts", listed[0])                         # the long text stays on the server
        self.assertNotIn("data", listed[0])

    def test_the_admin_can_add_too_and_everyone_sees_the_same_library(self):
        self.upload(self.admin)
        for c in (self.mei, self.raj, self.admin):
            self.assertEqual(len(c.get("/api/inbox/documents").json()["documents"]), 1)

    def test_bad_uploads_are_refused_with_a_reason(self):
        self.assertEqual(self.upload(self.mei, "notes.txt", b"hello", "text/plain").status_code, 422)
        self.assertEqual(self.upload(self.mei, "fake.pdf", b"not a pdf").status_code, 422)
        self.assertEqual(self.mei.post("/api/inbox/documents").status_code, 422)
        self.assertEqual(self.mei.get("/api/inbox/documents").json()["documents"], [])

    def test_a_failed_reading_shows_up_with_its_reason(self):
        self._digest.stop()
        with mock.patch.object(D, "_digest", mock.AsyncMock(side_effect=D.DigestFailed("The AI model cannot read PDFs."))):
            self.upload(self.mei)
        self._digest.start()
        d = self.mei.get("/api/inbox/documents").json()["documents"][0]
        self.assertEqual(d["status"], "failed")
        self.assertIn("cannot read", d["error"])

    def test_people_can_correct_what_the_ai_wrote_and_switch_it_off(self):
        doc = self.upload(self.mei).json()["document"]
        r = self.raj.put(f"/api/inbox/documents/{doc['id']}", json={"title": "60/70 sheet", "send_when": "Only when asked", "enabled": False})
        self.assertEqual(r.status_code, 200)
        got = self.admin.get("/api/inbox/documents").json()["documents"][0]
        self.assertEqual((got["title"], got["send_when"], got["enabled"]), ("60/70 sheet", "Only when asked", False))
        self.assertEqual(self.raj.put("/api/inbox/documents/nope", json={"title": "x"}).status_code, 404)
        self.assertEqual(self.raj.put(f"/api/inbox/documents/{doc['id']}", json={"title": "   "}).status_code, 422)

    def test_the_pdf_can_be_opened(self):
        doc = self.upload(self.mei).json()["document"]
        r = self.raj.get(f"/api/inbox/documents/{doc['id']}/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertEqual(r.content, PDF)
        self.assertEqual(self.raj.get("/api/inbox/documents/nope/file").status_code, 404)

    def test_only_the_admin_or_the_uploader_can_delete(self):
        doc = self.upload(self.mei).json()["document"]
        self.assertEqual(self.raj.delete(f"/api/inbox/documents/{doc['id']}").status_code, 403)
        self.assertEqual(len(self.mei.get("/api/inbox/documents").json()["documents"]), 1)
        self.assertEqual(self.mei.delete(f"/api/inbox/documents/{doc['id']}").status_code, 200)
        doc2 = self.upload(self.mei).json()["document"]
        self.assertEqual(self.admin.delete(f"/api/inbox/documents/{doc2['id']}").status_code, 200)
        self.assertEqual(self.admin.get("/api/inbox/documents").json()["documents"], [])

    def test_reading_can_be_retried(self):
        doc = self.upload(self.mei).json()["document"]
        r = self.mei.post(f"/api/inbox/documents/{doc['id']}/reprocess")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.mei.post("/api/inbox/documents/nope/reprocess").status_code, 404)

    def test_try_it_shows_the_reply_and_the_document_the_ai_would_send(self):
        doc = self.upload(self.mei).json()["document"]

        async def fake_handle(**kw):
            kw["trace"]["documents"] = [doc["id"]]
            return "Here is the datasheet."

        with mock.patch.object(self.main.llm_assistant, "handle_incoming_message", fake_handle):
            r = self.mei.post("/api/inbox/documents/test", json={"message": "send me the 60/70 datasheet"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["reply"], "Here is the datasheet.")
        self.assertEqual(body["documents"][0]["id"], doc["id"])
        self.assertEqual(body["documents"][0]["title"], DIGEST["title"])
        self.assertEqual(self.mei.post("/api/inbox/documents/test", json={"message": "  "}).status_code, 422)

    def test_the_page_opens_for_everyone_and_is_in_the_menu(self):
        for c in (self.admin, self.mei):
            r = c.get("/inbox/documents")
            self.assertEqual(r.status_code, 200)
            self.assertIn('id="doc-upload-input"', r.text)
            self.assertIn('id="doc-list"', r.text)
            self.assertIn('id="doc-try-input"', r.text)
            self.assertIn('href="/inbox/documents"', c.get("/inbox/chats").text)


if __name__ == "__main__":
    unittest.main()
