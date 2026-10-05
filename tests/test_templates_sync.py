"""New conversations are template-only. Approved Meta templates must really sync (they live under the WhatsApp
Business Account id, not the phone number id) and the send must be validated and saved so the chat can be opened."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import users_store as us
from fastapi.testclient import TestClient

APPROVED = {"name": "enquiry_followup", "category": "UTILITY", "language": "en_US", "status": "APPROVED",
            "components": [{"type": "BODY", "text": "Hello {{1}}, following up on {{2}}. Reply here."}]}
PAGE1 = {"data": [APPROVED, {**APPROVED, "name": "draft_one", "status": "PENDING"}],
         "paging": {"next": "https://graph.facebook.com/v22.0/WABA1/message_templates?after=XYZ"}}
PAGE2 = {"data": [{"name": "welcome_note", "category": "MARKETING", "language": "en", "status": "APPROVED",
                   "components": [{"type": "BODY", "text": "Welcome to Petrobind."}]},
                  {"name": "needs_image", "category": "MARKETING", "language": "en_US", "status": "APPROVED",
                   "components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Look"}]},
                  {"name": "has_button_var", "category": "UTILITY", "language": "en_US", "status": "APPROVED",
                   "components": [{"type": "BODY", "text": "Hi"}, {"type": "BUTTONS", "buttons": [{"type": "URL", "url": "https://x.com/{{1}}"}]}]}]}


class FakeGraph:
    calls = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    @staticmethod
    def _r(status, body):
        class R:
            status_code = status; content = b"{}"; text = str(body)
            def json(s): return body
        return R()

    async def get(self, url, params=None, headers=None, **k):
        FakeGraph.calls.append(("GET", url, params))
        if "after=XYZ" in url:
            return self._r(200, PAGE2)
        if "/WABA1/message_templates" in url:
            return self._r(200, PAGE1)
        return self._r(400, {"error": {"message": "Unsupported get request (wrong id)"}})

    async def post(self, url, json=None, headers=None, **k):
        FakeGraph.calls.append(("POST", url, json))
        return self._r(200, {"messages": [{"id": "wamid.T1"}]})


class Base(unittest.TestCase):
    def setUp(self):
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name)
        us._FILE = self._file = tmp.name
        FakeGraph.calls = []
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24
        main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        main.TEMPLATES_CACHE = None
        main._global_proactive_rate_limiter = lambda k: (True, 0)
        main._per_e164_rate_limiter = lambda k: (True, 0)
        self.saved = []

        async def record(**kw): self.saved.append(kw)

        self._p = [mock.patch.object(main, "WABA_ID", "WABA1"), mock.patch.object(main, "ACCESS_TOKEN", "tok"),
                   mock.patch.object(main, "PHONE_NUMBER_ID", "PNID1"), mock.patch.object(main.httpx, "AsyncClient", FakeGraph),
                   mock.patch.object(main, "_persist_outbound_safe", record), mock.patch.object(main._sb, "ENABLED", False)]
        for p in self._p: p.start()
        self.c = TestClient(main.app)
        self.c.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)

    def tearDown(self):
        for p in self._p: p.stop()
        self.main.TEMPLATES_CACHE = None
        if os.path.exists(self._file): os.remove(self._file)

    def sent(self):
        return [c for c in FakeGraph.calls if c[0] == "POST"]


class Sync(Base):
    def test_templates_are_read_from_the_business_account_all_pages_approved_only(self):
        r = self.c.get("/api/inbox/templates"); self.assertEqual(r.status_code, 200, r.text)
        names = sorted(t["name"] for t in r.json()["templates"])
        self.assertEqual(names, ["enquiry_followup", "has_button_var", "needs_image", "welcome_note"])  # both pages, no PENDING
        gets = [c for c in FakeGraph.calls if c[0] == "GET"]
        self.assertIn("/WABA1/message_templates", gets[0][1])
        self.assertNotIn("PNID1", gets[0][1])
        self.assertEqual(gets[0][2]["status"], "APPROVED"); self.assertEqual(gets[0][2]["limit"], 100)

    def test_unsupported_templates_are_flagged_with_a_reason(self):
        t = {x["name"]: x for x in self.c.get("/api/inbox/templates").json()["templates"]}
        self.assertTrue(t["enquiry_followup"]["supported"]); self.assertTrue(t["welcome_note"]["supported"])
        self.assertFalse(t["needs_image"]["supported"]); self.assertIn("image", t["needs_image"]["unsupported_reason"].lower())
        self.assertFalse(t["has_button_var"]["supported"])

    def test_a_missing_business_account_id_gives_a_clear_error(self):
        with mock.patch.object(self.main, "WABA_ID", ""):
            r = self.c.get("/api/inbox/templates")
        self.assertEqual(r.status_code, 503); self.assertIn("WHATSAPP_BUSINESS_ACCOUNT_ID", r.json()["detail"])

    def test_refresh_bypasses_the_cache(self):
        self.c.get("/api/inbox/templates"); n = len(FakeGraph.calls)
        self.c.get("/api/inbox/templates"); self.assertEqual(len(FakeGraph.calls), n)  # cached
        self.c.get("/api/inbox/templates?refresh=1"); self.assertGreater(len(FakeGraph.calls), n)


class NewConversationIsTemplateOnly(Base):
    def test_freeform_is_refused_even_if_someone_calls_the_api_directly(self):
        r = self.c.post("/api/inbox/new-conversation", json={"mode": "freeform_force", "e164": "+60123456789", "text": "hi"})
        self.assertEqual(r.status_code, 400); self.assertEqual(self.sent(), [])
        r = self.c.post("/api/inbox/new-conversation", json={"e164": "+60123456789", "text": "hi"})  # default mode
        self.assertEqual(r.status_code, 400); self.assertEqual(self.sent(), [])

    def test_sends_an_approved_template_with_ordered_params(self):
        r = self.c.post("/api/inbox/send-template", json={"template_name": "enquiry_followup", "language": "en_US",
                                                          "e164": "+60 12-345 6789", "params": {"1": "Ahmed", "2": "bitumen 60/70"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = self.sent()[0][2]
        self.assertEqual(body["to"], "60123456789")  # spaces, dashes and + removed
        self.assertEqual(body["template"]["name"], "enquiry_followup")
        self.assertEqual([p["text"] for p in body["template"]["components"][0]["parameters"]], ["Ahmed", "bitumen 60/70"])

    def test_params_beyond_nine_stay_in_numeric_order(self):
        order = [str(i) for i in range(1, 12)]
        self.main.TEMPLATES_CACHE = (__import__("time").time(), [{"name": "big", "language": "en_US", "status": "APPROVED", "supported": True,
            "components": [{"type": "BODY", "text": " ".join("{{%s}}" % i for i in order), "parameters": order}]}])
        r = self.c.post("/api/inbox/send-template", json={"template_name": "big", "language": "en_US", "e164": "60123456789",
                                                          "params": {i: "v" + i for i in order}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["text"] for p in self.sent()[0][2]["template"]["components"][0]["parameters"]], ["v" + i for i in order])

    def test_unknown_or_unapproved_template_is_refused_before_sending(self):
        for name in ("draft_one", "made_up"):
            r = self.c.post("/api/inbox/send-template", json={"template_name": name, "language": "en_US", "e164": "60123456789", "params": {}})
            self.assertEqual(r.status_code, 422, name); self.assertIn("approved", r.json()["detail"].lower())
        self.assertEqual(self.sent(), [])

    def test_unsupported_template_and_wrong_param_count_are_refused(self):
        r = self.c.post("/api/inbox/send-template", json={"template_name": "needs_image", "language": "en", "e164": "60123456789", "params": {}})
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/api/inbox/send-template", json={"template_name": "enquiry_followup", "language": "en_US", "e164": "60123456789", "params": {"1": "Ahmed"}})
        self.assertEqual(r.status_code, 422); self.assertIn("2", r.json()["detail"])
        self.assertEqual(self.sent(), [])

    def test_the_saved_chat_message_shows_the_real_text_so_the_chat_can_be_opened(self):
        self.c.post("/api/inbox/send-template", json={"template_name": "enquiry_followup", "language": "en_US", "e164": "60123456789",
                                                      "params": {"1": "Ahmed", "2": "bitumen"}})
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["text"], "Hello Ahmed, following up on bitumen. Reply here.")
        self.assertEqual(self.saved[0]["e164"], "60123456789")

    def test_new_conversation_endpoint_sends_templates_too(self):
        r = self.c.post("/api/inbox/new-conversation", json={"mode": "template_force", "template_name": "welcome_note", "language": "en", "e164": "60123456789"})
        self.assertEqual(r.status_code, 200, r.text); self.assertEqual(len(self.sent()), 1)


class Modal(Base):
    def test_modal_has_no_freeform_tab(self):
        html = self.c.get("/inbox/chats").text
        for gone in ('id="tab-freeform"', 'id="freeform-e164"', 'id="btn-freeform-send"', "Freeform (24h only)"):
            self.assertNotIn(gone, html)
        self.assertIn('id="template-select"', html); self.assertIn('id="btn-template-send"', html)


if __name__ == "__main__":
    unittest.main()
