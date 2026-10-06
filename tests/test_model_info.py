"""Ops page: which AI model is running, what it can take as input, which parameters it supports, whether the app's own
settings are compatible, every change of model, and live tests (text, image, document)."""
import asyncio
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import ai_health
import feedback_store as fs
import model_info
import users_store as us
from fastapi.testclient import TestClient

MODEL = {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini", "description": "Compact GPT-5.", "context_length": 400000,
         "architecture": {"input_modalities": ["text", "image", "file"], "output_modalities": ["text"], "modality": "text+image+file->text"},
         "pricing": {"prompt": "0.00000025", "completion": "0.000002"}, "top_provider": {"max_completion_tokens": 128000, "is_moderated": True},
         "supported_parameters": ["max_tokens", "reasoning", "reasoning_effort", "response_format", "seed", "tool_choice", "tools"],
         "default_parameters": {"temperature": None}, "knowledge_cutoff": "2024-05-31"}
TEXT_ONLY = {**MODEL, "id": "vendor/text-only", "name": "Text Only", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
             "supported_parameters": ["temperature", "max_tokens"]}


def run(c):
    return asyncio.run(c)


class FakeOpenRouter:
    models = [MODEL, TEXT_ONLY]
    fail = False
    calls = 0

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, **k):
        FakeOpenRouter.calls += 1
        if FakeOpenRouter.fail: raise ConnectionError("offline")
        class R:
            status_code = 200
            def json(s): return {"data": FakeOpenRouter.models}
        return R()


class Base(unittest.TestCase):
    def setUp(self):
        fs._REDIS_URL = fs._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name); fs._FILE = self._f = tmp.name
        FakeOpenRouter.models, FakeOpenRouter.fail, FakeOpenRouter.calls = [MODEL, TEXT_ONLY], False, 0
        model_info.reset()
        ai_health.reset()
        self._env = mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "openai/gpt-5-mini"})
        self._env.start()
        self._h = mock.patch.object(model_info.httpx, "AsyncClient", FakeOpenRouter); self._h.start()
        self._au = mock.patch.object(model_info._sb, "audit", mock.AsyncMock()); self._au.start()

    def tearDown(self):
        self._au.stop(); self._h.stop(); self._env.stop(); model_info.reset()
        if os.path.exists(self._f): os.remove(self._f)


class Describe(Base):
    def test_the_current_model_and_what_it_can_take_as_input(self):
        d = run(model_info.describe())
        self.assertEqual((d["model"], d["found"], d["name"]), ("openai/gpt-5-mini", True, "OpenAI: GPT-5 Mini"))
        self.assertEqual(d["input_modalities"], ["text", "image", "file"]); self.assertEqual(d["output_modalities"], ["text"])
        self.assertEqual((d["context_length"], d["max_completion_tokens"]), (400000, 128000))
        self.assertEqual(d["capabilities"], {"text": True, "images": True, "documents": True, "tools": True, "reasoning": True, "structured_outputs": False})
        self.assertEqual(d["supported_parameters"], sorted(MODEL["supported_parameters"]))
        self.assertEqual((d["pricing"]["input_per_million"], d["pricing"]["output_per_million"]), (0.25, 2.0))

    def test_it_says_which_of_the_apps_own_settings_the_model_ignores(self):
        d = run(model_info.describe())
        by = {p["name"]: p for p in d["app_parameters"]}
        self.assertFalse(by["temperature"]["supported"])                       # gpt-5-mini does not list temperature
        for ok in ("max_tokens", "tools", "tool_choice", "reasoning.effort"):
            self.assertTrue(by[ok]["supported"], ok)
        self.assertIn("ignored", by["temperature"]["note"].lower())

    def test_a_text_only_model_is_reported_as_unable_to_read_images_and_documents(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "vendor/text-only"}):
            d = run(model_info.describe(force=True))
        self.assertEqual((d["capabilities"]["images"], d["capabilities"]["documents"], d["capabilities"]["tools"]), (False, False, False))

    def test_an_unknown_model_is_flagged_not_crashed(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "nobody/nothing"}):
            d = run(model_info.describe(force=True))
        self.assertFalse(d["found"]); self.assertEqual(d["model"], "nobody/nothing"); self.assertIn("not listed", d["error"].lower())

    def test_the_list_is_cached_and_a_refresh_bypasses_the_cache(self):
        run(model_info.describe()); run(model_info.describe()); self.assertEqual(FakeOpenRouter.calls, 1)
        run(model_info.describe(force=True)); self.assertEqual(FakeOpenRouter.calls, 2)

    def test_when_openrouter_is_unreachable_the_last_known_answer_is_kept(self):
        run(model_info.describe())
        FakeOpenRouter.fail = True
        d = run(model_info.describe(force=True))
        self.assertTrue(d["found"]); self.assertTrue(d["stale"]); self.assertIn("offline", d["error"].lower())

    def test_can_read_uses_the_models_modalities(self):
        self.assertTrue(run(model_info.can_read("image"))); self.assertTrue(run(model_info.can_read("document")))
        with mock.patch.dict(os.environ, {"OPENROUTER_MODEL": "vendor/text-only"}):
            run(model_info.describe(force=True))
            self.assertFalse(run(model_info.can_read("image"))); self.assertFalse(run(model_info.can_read("document")))

    def test_when_nothing_is_known_it_does_not_block_media(self):
        FakeOpenRouter.fail = True
        self.assertTrue(run(model_info.can_read("image")))                    # unknown is treated as "try it"


class ModelChanges(Base):
    def test_the_first_use_and_every_change_are_recorded_with_what_the_model_can_do(self):
        run(model_info.record_change_if_needed("openai/gpt-5-mini"))
        run(model_info.record_change_if_needed("openai/gpt-5-mini"))          # same model: nothing new
        run(model_info.record_change_if_needed("vendor/text-only"))
        h = run(model_info.history())
        self.assertEqual([x["model"] for x in h], ["vendor/text-only", "openai/gpt-5-mini"])
        self.assertEqual(h[0]["from"], "openai/gpt-5-mini"); self.assertEqual(h[0]["input_modalities"], ["text"])
        self.assertEqual(h[1]["input_modalities"], ["text", "image", "file"]); self.assertTrue(h[0]["at"])

    def test_noting_a_model_used_by_a_call_triggers_the_record(self):
        async def go():
            model_info.note_model_used("openai/gpt-5-mini")
            await asyncio.sleep(0.05)
            return await model_info.history()
        self.assertEqual([x["model"] for x in run(go())], ["openai/gpt-5-mini"])


class Probes(Base):
    def client(self, answer, fail=None):
        async def create(**kw):
            self.sent = kw
            if fail: raise fail
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=answer))])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    def probe(self, kind, answer, fail=None):
        with mock.patch.object(model_info, "_client", lambda: self.client(answer, fail)):
            return run(model_info.probe(kind))

    def test_text_probe(self):
        r = self.probe("text", "OK"); self.assertTrue(r["ok"]); self.assertIn("ms", r); self.assertEqual(r["model"], "openai/gpt-5-mini")

    def test_image_probe_sends_a_real_picture_and_checks_the_answer(self):
        r = self.probe("image", "4821")
        self.assertTrue(r["ok"]); parts = self.sent["messages"][-1]["content"]
        img = [p for p in parts if p.get("type") == "image_url"][0]["image_url"]["url"]
        self.assertTrue(img.startswith("data:image/png;base64,"))
        self.assertFalse(self.probe("image", "I can see something")["ok"])

    def test_document_probe_sends_a_real_pdf_and_checks_the_answer(self):
        r = self.probe("document", "The code is 4821"); self.assertTrue(r["ok"])
        part = [p for p in self.sent["messages"][-1]["content"] if p.get("type") == "file"][0]["file"]
        self.assertTrue(part["file_data"].startswith("data:application/pdf;base64,")); self.assertTrue(part["filename"].endswith(".pdf"))

    def test_a_failing_probe_reports_the_error_and_text_failures_flag_the_outage(self):
        class Credits(Exception):
            status_code = 402
        r = self.probe("text", "", fail=Credits("Insufficient credits"))
        self.assertFalse(r["ok"]); self.assertIn("credits", r["error"].lower())
        self.assertFalse(ai_health.current()["ok"]); self.assertEqual(ai_health.current()["kind"], "credits")

    def test_a_working_text_probe_clears_the_outage(self):
        ai_health.record_failure(kind="error", message="x")
        self.probe("text", "OK"); self.assertTrue(ai_health.current()["ok"])

    def test_unknown_probe_kind(self):
        with self.assertRaises(ValueError): run(model_info.probe("video"))

    def test_the_test_pictures_are_valid_files(self):
        png = model_info.test_png("4821"); self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        pdf = model_info.test_pdf("PETROBIND TEST 4821"); self.assertTrue(pdf.startswith(b"%PDF-1.4")); self.assertIn(b"4821", pdf); self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))


class AdminApi(Base):
    def setUp(self):
        super().setUp()
        us._REDIS_URL = us._REDIS_TOKEN = ""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False); tmp.close(); os.remove(tmp.name); us._FILE = self._u = tmp.name
        import main
        self.main = main
        main.INBOX_ADMIN_TOKEN = "x" * 24; main._sb.ENABLED = False; main._LOGIN_FAILS.clear(); main._USERS_CACHE.clear()
        run(us.create_user(username="mei", name="Mei Ling", password="mei-password-1")); run(main._refresh_users_cache())
        self.admin = TestClient(main.app); self.admin.post("/inbox/login", data={"username": "", "password": "x" * 24}, follow_redirects=False)
        self.agent = TestClient(main.app); self.agent.post("/inbox/login", data={"username": "mei", "password": "mei-password-1"}, follow_redirects=False)

    def tearDown(self):
        if os.path.exists(self._u): os.remove(self._u)
        super().tearDown()

    def test_admin_only(self):
        anon = TestClient(self.main.app)
        for m, p in (("get", "/api/inbox/admin/model"), ("post", "/api/inbox/admin/model/test")):
            self.assertEqual(getattr(anon, m)(p).status_code, 401); self.assertEqual(getattr(self.agent, m)(p).status_code, 403)

    def test_the_model_report(self):
        j = self.admin.get("/api/inbox/admin/model").json()
        self.assertEqual(j["model"], "openai/gpt-5-mini"); self.assertTrue(j["capabilities"]["images"])
        self.assertIn("health", j); self.assertTrue(j["health"]["ok"]); self.assertEqual(j["history"][0]["model"], "openai/gpt-5-mini")
        self.assertIn("openrouter.ai", j["source"])
        self.admin.get("/api/inbox/admin/model?refresh=1"); self.assertGreaterEqual(FakeOpenRouter.calls, 2)

    def test_the_test_button(self):
        async def create(**kw):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="4821"))])
        fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        with mock.patch.object(model_info, "_client", lambda: fake):
            r = self.admin.post("/api/inbox/admin/model/test", json={"kind": "image"})
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        self.assertEqual(self.admin.post("/api/inbox/admin/model/test", json={"kind": "video"}).status_code, 422)

    def test_the_ops_page_has_the_panel(self):
        h = self.admin.get("/inbox/admin").text
        for needle in ('id="model-panel"', 'id="model-name"', 'id="model-params"', 'id="model-history"', 'data-model-test="image"', 'data-model-test="document"'):
            self.assertIn(needle, h)


if __name__ == "__main__":
    unittest.main()
