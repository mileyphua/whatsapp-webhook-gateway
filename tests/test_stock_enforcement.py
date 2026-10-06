"""An item switched Off on the Stock page is out of stock: the AI must not mention, list, compare, offer or recommend it,
and if a buyer asks for it by name it says plainly that it is not available and suggests an in-stock alternative."""
import asyncio
import importlib
import json
import os
import types
import unittest
from unittest import mock

os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

import ai_health
import catalog
import conversation_store as cs
import feedback_store as fs
import learning
import llm_assistant as L
from rag.retrieve import RetrievedChunk

R = importlib.import_module("rag.retrieve")
PH = "60177700077"


def run(c):
    return asyncio.run(c)


class Base(unittest.TestCase):
    def setUp(self):
        ai_health.reset()
        cs._REDIS_URL = cs._REDIS_TOKEN = ""; fs._REDIS_URL = fs._REDIS_TOKEN = ""
        fs._FILE = "/tmp/_stock_enf.json"
        if os.path.exists(fs._FILE): os.remove(fs._FILE)
        learning.invalidate_cache(); catalog.invalidate(); R.load_index_if_needed()
        self.llm_calls, self.systems, self.retrieve_kw = 0, [], []

    def tearDown(self):
        if os.path.exists(fs._FILE): os.remove(fs._FILE)
        catalog.invalidate()

    def off(self, *slugs):
        for s in slugs: run(catalog.set_item(s, False, "test"))

    def chunk(self, slug_url_title="60-70", text="Bitumen 60/70: penetration 60-70 dmm."):
        return RetrievedChunk(url="https://www.petrobindglobal.com/products/bitumen-" + slug_url_title, title="Bitumen 60/70 Supplier | COA/PDS", chunk_text=text, similarity=0.9, chunk_index=0)

    def turn(self, text, reply="OK.", refs=None, message_count=3):
        plan = json.dumps({"intent": "x", "needs_human": False, "skills": [], "points": [], "avoid": [], "tone": "casual"})
        seq = [plan, reply]; n = {"i": 0}

        async def create(**kw):
            self.llm_calls += 1
            self.systems.append("\n".join(m["content"] for m in kw.get("messages", []) if m.get("role") == "system"))
            t = seq[min(n["i"], 1)]; n["i"] += 1
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=t, tool_calls=None), finish_reason="stop")])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

        async def retrieve(q, **kw):
            self.retrieve_kw.append(kw); return refs if refs is not None else [self.chunk("80-100", "Bitumen 80/100 is a softer paving grade.")]

        cs._SESSIONS.pop(PH, None)
        s = cs.ConversationSession(phone_number=PH); s.message_count = message_count
        s.history += [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi, how can I help?"}]
        cs._SESSIONS[PH] = s
        with mock.patch.object(L, "_openrouter_client", return_value=client), mock.patch.object(L, "retrieve", retrieve), mock.patch.object(L, "index_is_ready", lambda: True):
            return run(L.handle_incoming_message(phone_number=PH, inbound_text=text, draft_only=False)), cs._SESSIONS[PH]


class ScrubbingText(Base):
    def scrub(self, text, *slugs):
        self.off(*slugs); return catalog.scrub_text(text, run(catalog.disabled_items()))

    def test_a_related_grades_card_is_removed_with_its_description(self):
        page = [c for c in R.page_chunks("https://www.petrobindglobal.com/products/bitumen-60-70")][3].chunk_text
        out = self.scrub(page, "products-bitumen-35-50")
        self.assertNotIn("35/50", out)
        self.assertNotIn("A durable paving binder for highways, airport works", out)      # that card's description
        self.assertIn("View 30/40 specs", out); self.assertIn("40/50", out)

    def test_an_enumeration_loses_only_the_off_grade(self):
        out = self.scrub("Typical grades include R75/35, R85/35, R85/40 and R90/15 for waterproofing.", "products-oxidized-r85-35")
        self.assertNotIn("R85/35", out)
        for keep in ("R75/35", "R85/40", "R90/15", "waterproofing"): self.assertIn(keep, out)
        self.assertNotIn(", ,", out); self.assertNotIn(" ,", out)

    def test_a_sentence_about_only_the_off_grade_is_dropped(self):
        out = self.scrub("Bitumen 60/70 is our flagship grade. It suits roads and highways.", "products-bitumen-60-70")
        self.assertNotIn("60/70", out); self.assertIn("It suits roads", out)

    def test_in_stock_items_are_untouched_and_nothing_off_changes_nothing(self):
        text = "Bitumen 80/100 and 60/70 are paving grades."
        self.assertEqual(catalog.scrub_text(text, []), text)
        self.assertEqual(self.scrub(text, "products-oxidized-r85-35"), text)

    def test_an_unavailable_statement_may_name_the_item(self):
        out = self.scrub("Sorry, 60/70 isn't available right now. 80/100 is a close option.", "products-bitumen-60-70")
        self.assertIn("60/70 isn't available", out); self.assertIn("80/100", out)

    def test_similar_names_are_not_touched(self):
        out = self.scrub("We supply SS-1, CSS-1 and HFMS-1 emulsions.", "products-bitumen-emulsion-ms-1")
        self.assertEqual(out, "We supply SS-1, CSS-1 and HFMS-1 emulsions.")


class RetrievalHidesOffPages(Base):
    def test_the_off_items_page_is_excluded_from_retrieval(self):
        self.off("products-bitumen-60-70")
        self.turn("tell me about bitumen 80/100", "Fine.")
        self.assertEqual(self.retrieve_kw[-1].get("exclude_slugs"), {"products-bitumen-60-70"})

    def test_nothing_off_keeps_retrieval_exactly_as_before(self):
        self.turn("tell me about bitumen 80/100", "Fine.")
        self.assertFalse(self.retrieve_kw[-1].get("exclude_slugs"))

    def test_retrieve_itself_skips_excluded_pages(self):
        import numpy as np
        slug = "products-bitumen-60-70"
        idx = [i for i, c in enumerate(R._index) if c.get("page_slug") == slug]
        sims = np.zeros(len(R._index), dtype=np.float32); sims[idx] = 0.99; sims[0] = 0.5
        fake = types.SimpleNamespace(embeddings=types.SimpleNamespace(create=mock.AsyncMock(return_value=types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.1] * 1536)]))))
        with mock.patch.object(R, "_openai_embeddings_client", lambda: fake), mock.patch.object(R, "_cosine", lambda q: sims):
            all_hits = run(R.retrieve("60/70"))
            hits = run(R.retrieve("60/70", exclude_slugs={slug}))
        self.assertTrue(any(h.page_slug == slug for h in all_hits))
        self.assertFalse(any(h.page_slug == slug for h in hits)); self.assertTrue(hits)


class AskingForAnOffItem(Base):
    def test_naming_an_off_grade_gets_unavailable_plus_a_close_alternative_without_calling_the_ai(self):
        self.off("products-bitumen-60-70")
        for text in ("do you have bitumen 60/70?", "what are the specs of 60-70?", "I need 60/70"):
            out, s = self.turn(text, "SHOULD NOT BE USED")
            self.assertIn("isn't available", out); self.assertIn("60/70", out)
            self.assertTrue("50/70" in out or "80/100" in out, out)
            self.assertEqual(self.llm_calls, 0, text); self.assertTrue(out.rstrip().endswith("?"))
            self.assertTrue(s.more_info_url.endswith(("bitumen-50-70", "bitumen-80-100")), s.more_info_url)

    def test_yes_then_sends_the_alternatives_page(self):
        self.off("products-bitumen-60-70")
        _, s = self.turn("do you have 60/70?")
        out = L._more_info_followup(s, "yes please")
        self.assertIn(s.more_info_url, out)

    def test_a_whole_family_off_gets_a_family_level_reply(self):
        run(catalog.set_family("oxidized", False, "t"))
        out, _ = self.turn("tell me about oxidized bitumen")
        self.assertIn("isn't available", out.replace("aren't", "isn't")); self.assertEqual(self.llm_calls, 0)
        self.assertNotIn("R85/40", out)

    def test_naming_an_in_stock_grade_is_answered_normally(self):
        self.off("products-bitumen-60-70")
        out, _ = self.turn("what is bitumen 80/100 used for?", "It is a softer paving grade for cooler climates.", refs=[self.chunk("80-100", "80/100 text")])
        self.assertGreaterEqual(self.llm_calls, 1); self.assertIn("softer paving grade", out)

    def test_base_oil_off_still_answers_other_questions_and_keeps_contact_info(self):
        self.off("products-base-oil-sn150")
        out, _ = self.turn("do you supply base oil SN150?")
        self.assertIn("isn't available", out); self.assertEqual(self.llm_calls, 0)
        self.assertNotIn("products-base-oil-sn150", run(catalog.hidden_slugs()))


class TheAiNeverListsOffItems(Base):
    def test_the_prompt_lists_what_is_out_of_stock(self):
        self.off("products-bitumen-60-70", "products-oxidized-r85-35")
        self.turn("what grades do you have?", "We have several paving grades.")
        p = " ".join(self.systems[-1].split())
        self.assertIn("OUT OF STOCK", p); self.assertIn("Bitumen 60/70", p); self.assertIn("R85/35", p)
        self.assertIn("never mention", p.lower())

    def test_no_out_of_stock_block_when_everything_is_on(self):
        self.turn("what grades do you have?", "We have several paving grades.")
        self.assertNotIn("OUT OF STOCK", self.systems[-1])

    def test_a_reply_that_lists_an_off_grade_anyway_is_cleaned(self):
        self.off("products-bitumen-60-70")
        out, _ = self.turn("what paving grades do you have?", "We supply 30/40, 40/50, 60/70 and 80/100 paving grades.\n\nBitumen 60/70 is our most popular.")
        self.assertNotIn("60/70", out)
        for keep in ("30/40", "40/50", "80/100"): self.assertIn(keep, out)

    def test_the_reply_is_never_empty_after_cleaning(self):
        self.off("products-bitumen-60-70")
        out, _ = self.turn("what do you recommend?", "Bitumen 60/70 is the best choice.")
        self.assertTrue(out.strip()); self.assertNotIn("60/70", out)

    def test_the_which_product_question_skips_families_that_are_off(self):
        run(catalog.set_family("oxidized", False, "t")); run(catalog.set_family("emulsion", False, "t"))
        reply = L.generic_spec_reply(run(catalog.disabled_items()))
        self.assertNotIn("Oxidized", reply); self.assertNotIn("Emulsion", reply)
        self.assertIn("Bitumen", reply); self.assertIn("Polymer", reply)

    def test_an_offer_for_a_page_that_has_since_gone_off_is_not_sent(self):
        _, s = self.turn("tell me more about bitumen 80/100", "Softer paving grade.", refs=[self.chunk("80-100", "80/100 text")])
        self.assertTrue(s.more_info_url.endswith("bitumen-80-100"))
        self.off("products-bitumen-80-100")
        out, _ = self.turn("yes", "Sure, which grade do you need?")
        self.assertNotIn("bitumen-80-100", out)


if __name__ == "__main__":
    unittest.main()
