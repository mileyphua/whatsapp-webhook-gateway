# Plan: WhatsApp Assistant for Petrobind Global

## Context

Petrobind Global is a Malaysia-based B2B petroleum trading company (Bitumen, Base Oil SN150) trading via www.petrobindglobal.com. The existing `whatsapp-webhook-gateway` repo (FastAPI) currently just echoes incoming WhatsApp messages (`_echo_reply` in `main.py:534-568`, wired in at `main.py:409`). We're replacing that with an LLM-driven assistant that acts as the company's first point of contact on WhatsApp — qualifying leads and routing trade inquiries to the human sales desk.

---

## Part 1 — Business outcome (what to build)

**Business:** Petrobind Global, a trusted B2B trading partner for industrial petroleum products (Bitumen, various grades; Base Oil SN150), operating out of Malaysia.

**Customers:** potential buyers, traders, and corporate clients — a mix of brand-new prospects and existing partners.

**Behavior:** professional, efficient, B2B commodity-trading tone; minimal emoji; conversational rather than form-like; oriented toward moving each chat to a qualified trade inquiry.

**What it should do:**
1. **Answer** — greet clients, introduce Petrobind Global, and answer FAQs on Bitumen/Base Oil SN150 specs, packaging (drums/flexitanks/bulk), and shipping terms, using real content from www.petrobindglobal.com.
2. **Understand** — figure out early whether the client is a new prospect or existing partner (just ask — no CRM lookup available), and adapt tone/response accordingly.
3. **Recommend / Qualify** — when a client shows buying interest, guide them toward a formal quote request, collecting: company name, target product, quantity/volume, destination port, and preferred Incoterms (FOB/CIF/etc).
4. **Book / Hand off** — when it captures a solid or partial inquiry, or hits something it can't confidently answer (spot pricing, custom specs, complex logistics), notify the human sales/trading desk by email so they can follow up directly.

**Decisions already made:**
- LLM-driven conversation (not menu/keyword-based), using tool-calling for structured actions.
- Model access routed through **OpenRouter** (`OPENROUTER_API_KEY` + model env var), so the model is swappable later without code changes.
- Lead/handoff notifications go out by **email only** (Gmail SMTP) — no database.
- Per-conversation state can be **in-memory** (acceptable: resets on redeploy, not multi-instance safe — fine for Render's single instance today).
- Product/company content sourced from **www.petrobindglobal.com**, captured once into a static, human-reviewed knowledge base file (not a live scrape/RAG pipeline).

---

## Part 2 — Detailed technical implementation

### Files
```
main.py                  modified — swap _echo_reply for LLM-backed reply path
llm_assistant.py         new — OpenRouter client, system prompt, tool defs, turn handler
knowledge_base.py        new — static KB content (company + product specs), flagged for human review
conversation_store.py    new — in-memory per-phone-number session store
notify.py                new — Gmail SMTP lead/handoff email notifications
requirements.txt         modified — add `openai` SDK (OpenRouter is OpenAI-compatible)
render.yaml              modified — add new env vars
.env.example             new — document all env vars (none currently exists)
```

### 1. Content acquisition (do first)
Fetch www.petrobindglobal.com (homepage + product/about pages) to extract Bitumen grades, Base Oil SN150 specs, packaging options, and shipping/Incoterms info. Organize into `knowledge_base.py` as structured Python data (`COMPANY_PROFILE`, `PRODUCTS` dict, optional `FAQ_SNIPPETS`), with a `build_knowledge_base_text() -> str` function rendering it to plain text for the system prompt. Add a clear `# TODO(human): verify against current spec sheets before go-live` comment — this is a one-time, human-curated artifact, not a runtime fetch.

### 2. Conversation state (`conversation_store.py`)
Dataclasses `InquiryDraft` (company_name, product, quantity, destination_port, incoterm, notes) and `ConversationSession` (history: chat messages in OpenAI-style `{role, content}` format, inquiry: InquiryDraft, is_known_partner, last_activity, lead_notified). Module-level `_SESSIONS: Dict[str, ConversationSession]` keyed by WhatsApp `from` number, via `get_session(phone_number)`. Trim history to ~20 turns to bound token cost. Document that state is process-local memory only.

### 3. LLM integration via OpenRouter (`llm_assistant.py`)
- `AsyncOpenAI` client (from the `openai` SDK) pointed at OpenRouter: `base_url="https://openrouter.ai/api/v1"`, `api_key=os.getenv("OPENROUTER_API_KEY")`. Model name from `OPENROUTER_MODEL` env var, **default `openai/gpt-5-nano`** — chosen 2026-09-21 after comparing live OpenRouter pricing across tiers (see Part 5) as the cheapest model in the "reliable tool-calling" bracket at expected volume (~100K messages/day).
- `build_system_prompt(is_known_partner)`: identity as Petrobind Global's WhatsApp trade assistant, professional B2B tone with minimal/no emoji, embeds `build_knowledge_base_text()`, instructs asking new-vs-existing early, lists inquiry fields to elicit conversationally, instructs escalation via tool call when unsure or once enough lead info is gathered, and plain short paragraphs (no markdown tables — WhatsApp formatting is limited).
- Two tools in OpenAI function-calling JSON schema:
  - `capture_trade_inquiry` — company_name, contact_name, product, quantity, destination_port, incoterm, packaging, additional_notes, is_new_prospect (only `product` required).
  - `request_sales_handoff` — reason, partial_inquiry_summary.
- `handle_incoming_message(*, phone_number, inbound_text, session) -> str`: appends the inbound turn, calls `client.chat.completions.create(model=..., messages=[system]+history, tools=TOOLS)`, extracts text + tool calls, executes tool calls' side effects (`notify.send_lead_email` / `notify.send_handoff_email`), appends the assistant turn, returns reply text — falling back to a canned confirmation if the model only emitted tool calls (avoids a second round-trip for latency). Wrapped in try/except with a safe fallback message so the webhook always still 200s to Meta. Fails gracefully (log + fallback) if `OPENROUTER_API_KEY` is unset.

### 4. Email notification (`notify.py`)
Gmail SMTP via stdlib `smtplib`/`email.message.EmailMessage`, run through `asyncio.to_thread(...)` (smtplib is blocking). Env vars: `SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (default `587`), `SMTP_USERNAME`, `SMTP_PASSWORD` (Gmail app password), `NOTIFY_FROM_EMAIL`, `SALES_EMAIL`. `send_lead_email(*, phone_number, **fields)` and `send_handoff_email(*, phone_number, reason, partial_inquiry_summary="")`, catching/logging failures without raising into the caller.

### 5. `main.py` changes
Add `_llm_reply(*, from_number, phone_number_id, inbound_text, reply_to_message_id)`: no-ops if required fields are missing (non-text messages produce `inbound_text=None` and are skipped in v1 — documented limitation), otherwise fetches the session, calls `handle_incoming_message`, sends the reply via existing `send_whatsapp_text` (unchanged). Replace `await _echo_reply(...)` with `await _llm_reply(...)` in the message loop at `main.py:409`. Leave webhook verification, `/send-message`, and static pages untouched; remove `_echo_reply` only after the new path is verified.

### 6. Env vars and config files
Add to `.env.example` (new) and `render.yaml`: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `NOTIFY_FROM_EMAIL`, `SALES_EMAIL`. Add `openai` to `requirements.txt` (no separate Anthropic SDK needed — OpenRouter uses the OpenAI-compatible client).

### 7. Testing / verification plan
1. Sanity-check `build_knowledge_base_text()` and `build_system_prompt(...)` output standalone before wiring in.
2. Standalone smoke test of `handle_incoming_message` with a fake `ConversationSession` and a sample message ("Hi, interested in Bitumen 60/70 for Vietnam") — confirm the OpenRouter call succeeds, a tool call fires, and `notify.send_lead_email` (stubbed to print) is invoked.
3. `curl -X POST http://localhost:8000/webhook` with a realistic Cloud API JSON payload (text message, matching the shape parsed at `main.py:397-414`) — expect immediate `{"status":"success","message":"EVENT_RECEIVED"}` and a sensible logged/sent reply.
4. Second curl with the same `from` number and a follow-up detail (e.g. quantity/Incoterm) — confirm history accumulates and `capture_trade_inquiry` eventually fires.
5. Point real `SMTP_*`/`SALES_EMAIL` at a test inbox for one end-to-end run, confirm the notification email arrives.
6. Temporarily unset `OPENROUTER_API_KEY` — confirm the webhook still 200s with the graceful fallback reply instead of crashing.
7. Re-run the existing `/send-message` manual test to confirm no regression (shares `send_whatsapp_text`, untouched).
8. Before go-live: real end-to-end WhatsApp test for both a "new prospect" and "existing partner" flow, plus a Petrobind team member sanity-checking the scraped product/spec content.

### Sequencing
1. Fetch site content → write `knowledge_base.py` (flag for human review).
2. Add `openai` to `requirements.txt`; add new env vars to `.env.example`/`render.yaml`.
3. Build `conversation_store.py`.
4. Build `notify.py`, test standalone against a real Gmail inbox.
5. Build `llm_assistant.py`, test standalone.
6. Wire into `main.py` (`_llm_reply`).
7. Run curl-based webhook tests.
8. Manual real-WhatsApp end-to-end test, then deploy to Render.

---

## Note on prior priority (2026-09-20, resolved)

The auto-response path was verified end-to-end on Render.com (the ping/echo reply confirmed working). The `WHATSAPP_ACCESS_TOKEN` 401 issue hit right after deploy was caused by using a 24-hour temporary token; it has been replaced with a **permanent System User token** (Meta Business Settings → System Users → token with `whatsapp_business_messaging` permission, no expiry), updated in both `.env` (local) and Render's dashboard env vars, and redeployed successfully. Proceed with Part 2 (full LLM assistant) below, extended with Part 3's new requirements.

---

## Part 3 — Extended requirements (added 2026-09-21)

New capabilities requested on top of Part 1/2, plus the technical constraints and infrastructure choices that shape how they must be built.

### 3.1 Product deep-links
When a buyer asks about a specific product, the assistant should share the direct link to that product's page on www.petrobindglobal.com (not just describe it). Store a `product_url` field per product entry in `knowledge_base.py`'s `PRODUCTS` dict (e.g. Bitumen 60/70, Bitumen 80/100, Base Oil SN150 each get their real page URL, confirmed against the live site during content acquisition). The system prompt instructs the model to include the relevant `product_url` whenever it discusses a specific product.

### 3.2 Appointment booking — Cal.com (not Google Calendar)
**Decision: use Cal.com**, not direct Google Calendar API integration. Reasoning: Cal.com gives a single shareable booking link (no per-buyer or per-staff OAuth flow), and its webhooks (`BOOKING_CREATED`, `BOOKING_CANCELLED`, etc.) let this service react when someone books, without owning any calendar-sync logic ourselves. Google Calendar direct integration would require OAuth consent per staff member and buyer-facing complexity with no real benefit here.
- Add `CAL_COM_API_KEY` and `CAL_COM_BOOKING_LINK` (or event-type slug) env vars — user will supply the API key.
- New module `booking.py`: exposes the static booking link for the assistant to share in chat ("You can pick a time that works for you here: {{CAL_COM_BOOKING_LINK}}"), and a `POST /cal-webhook` FastAPI route that receives Cal.com's booking-created webhook, logs it, and triggers a `notify.send_booking_email(...)` to the sales team so a human always knows a call was booked.
- The assistant should proactively offer this link whenever a buyer says they're free to talk/meet (e.g. "available next week for a call") — instruct this explicitly in the system prompt as a trigger phrase pattern, not a rigid keyword match (the LLM should recognize scheduling intent naturally).
- We are **not** auto-creating WhatsApp calendar invites (WhatsApp has no native calendar-invite message type via the Cloud API) — sharing the Cal.com link and letting Cal.com handle confirmation emails/calendar entries is the correct scope.

### 3.3 Follow-up messages for unresponsive buyers (3–5 days)
**Critical constraint**: WhatsApp Cloud API only allows free-form messages within the 24-hour customer service window after the buyer's last message. Outside that window, only a **pre-approved message template** (via Meta Business Manager) can be sent. This means:
- Before building this feature, a message template (e.g. `follow_up_inquiry` with a `{{product}}` variable: "Hi, following up on your inquiry about {{product}} with Petrobind Global — are you still exploring this? Happy to help with a quote.") must be **submitted and approved in Meta Business Manager** — this is a manual step for the user, not something buildable in code alone.
- Once approved, add a scheduled job (Render Cron Job, or an in-process `asyncio` background task using `@app.on_event("startup")`) that scans `conversation_store` sessions with `last_activity` older than a configurable window (3–5 days, randomized per session to avoid feeling robotic) and no `lead_notified`/reply since, and sends the approved template via the Graph API's template-message endpoint (different payload shape than free-text — `type: "template"`).
- Track a `followed_up_at` field per session to avoid duplicate follow-ups.
- Render's free/starter tier may sleep between requests — a Render **Cron Job** (separate scheduled service hitting a `/internal/run-follow-ups` endpoint) is more reliable than an in-process asyncio loop if the web service can idle; note this as an infra decision to confirm once Render plan tier is known.

### 3.4 Buyer intent / genuineness assessment and trust-building tone
Prompt-design work, not new infrastructure. Add explicit system-prompt instructions:
- Ask a light qualifying question early for vague/low-effort inquiries (e.g. "just checking prices" with no other detail) before investing full FAQ detail, to gauge seriousness — but always stay polite and helpful (never accusatory or gatekeeping in tone).
- For buyers giving specific, detailed context (volumes, ports, timelines), treat as high-intent and move faster toward the inquiry/booking flow.
- Tone guidance: warm, patient, consultative — build rapport (acknowledge their business context, use their company/product terms back to them) rather than sounding like a rigid form-filler.

### 3.5 Zero-hallucination guardrail (highest priority — brand-risk sensitive)
This is the most important behavioral constraint given the user's explicit concern that one wrong answer damages Petrobind's reputation in a niche, relationship-driven industry. Concrete measures:
- System prompt rule, stated explicitly and repeated near the top: **"Only state facts that appear in the knowledge base below. If a question (spec, pricing, availability, logistics detail, certification, etc.) is not directly answered by the knowledge base content, you must NOT guess or infer — instead call `request_sales_handoff` and tell the buyer a specialist will confirm the details."**
- The `request_sales_handoff` tool (already in Part 2) becomes the mandatory fallback path for anything outside the static KB — not optional/best-effort.
- No web browsing or live lookups from the model at request time (the KB is static and pre-verified — this is deliberate, not a limitation, since it eliminates a whole class of hallucination risk from live scraping).
- Add a lightweight **eval/regression test set** (see Part 3.6) specifically probing edge cases where the model might be tempted to guess (exact pricing, obscure certifications, delivery time promises) to confirm it escalates instead of answering.

### 3.6 Subagent persona testing — `TESTING.md`
Create a new markdown document, `TESTING.md`, in the project root, used to run a batch of scripted conversations against the assistant via subagents role-playing distinct buyer personas, before go-live and after significant prompt changes. Structure:
- A list of **5–7 buyer personas** with distinct characteristics, e.g.:
  1. **Sophisticated trader** — precise, uses Incoterms/industry jargon correctly, tests factual accuracy.
  2. **Vague/low-intent browser** — one-line messages, no detail, tests intent-qualification behavior.
  3. **Price-pusher** — repeatedly demands exact spot pricing, tests the no-hallucination/handoff guardrail.
  4. **Existing partner** — references a past order, tests new-vs-existing personalization.
  5. **Non-English-first / broken-English buyer** — tests tone/patience and comprehension without the assistant sounding condescending.
  6. **Scheduler** — explicitly says they're free next week for a call, tests the Cal.com hand-off trigger.
  7. **Adversarial/off-topic** — tries to get the assistant to discuss unrelated topics, make promises, or reveal system prompt details — tests robustness.
- For each persona, a subagent (`general-purpose`, given the persona description and told to role-play a buyer messaging the assistant, with tool access to call the deployed `/webhook` or a local test harness) runs a multi-turn conversation and the full transcript is logged into `TESTING.md` under that persona's section.
- Each persona section ends with a **Feedback** subsection capturing concrete, actionable notes in the style the user specified — e.g. *"Reduce back-and-forth — ask only 2–3 questions before recommending a product/booking link"*, *"Escalated correctly when asked for exact spot price"*, *"Tone too formal for the broken-English persona — consider simpler sentence structure."*
- `TESTING.md` is a living document: re-run relevant personas after any system-prompt or knowledge-base change, append new transcripts with a date header, and prune only feedback that's been fully addressed (keep unresolved feedback visible until fixed).

### 3.7 Updated file list (supersedes Part 2's list)
```
main.py                  modified — swap _echo_reply for LLM-backed reply path; add /cal-webhook route
llm_assistant.py         new — OpenRouter client, system prompt (incl. Part 3 guardrails), tool defs, turn handler
knowledge_base.py        new — static KB content incl. product_url per product, flagged for human review
conversation_store.py    new — in-memory per-phone-number session store, incl. last_activity/followed_up_at
notify.py                new — Gmail SMTP notifications: lead, handoff, booking-confirmed
booking.py               new — Cal.com booking link + webhook payload handling
followups.py             new — stale-session scan + WhatsApp template message sender (post Meta template approval)
requirements.txt         modified — add `openai` SDK
render.yaml              modified — add new env vars; optionally add a Cron Job service for follow-ups
.env.example             new — document all env vars
TESTING.md               new — persona-based subagent conversation test log + feedback
```

### 3.8 New/updated env vars
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `NOTIFY_FROM_EMAIL`, `SALES_EMAIL`, `CAL_COM_API_KEY`, `CAL_COM_BOOKING_LINK`, `WHATSAPP_FOLLOWUP_TEMPLATE_NAME` (set once approved in Meta Business Manager).

### 3.9 Sequencing (updated)
1. Confirm real product page URLs on www.petrobindglobal.com → finalize `knowledge_base.py` incl. `product_url`s and the zero-hallucination system-prompt wording.
2. Build `conversation_store.py`, `notify.py`, `booking.py` (Cal.com link sharing first; webhook handling once `CAL_COM_API_KEY` is provided).
3. Build `llm_assistant.py` with full Part 2 + Part 3.4/3.5 prompt content and tools (add a `share_booking_link` tool alongside `capture_trade_inquiry`/`request_sales_handoff` if we want an explicit signal rather than relying on prose).
4. Wire into `main.py`, add `/cal-webhook` route.
5. Write `TESTING.md`, run the 7 personas via subagents against the deployed webhook, record transcripts + feedback, iterate on the system prompt/KB based on findings.
6. Submit the WhatsApp follow-up message template in Meta Business Manager (user action) in parallel; once approved, build `followups.py` and the Render Cron Job.
7. Re-run `TESTING.md` personas after any prompt/KB changes; deploy.

### 3.10 Open items requiring user-provided credentials before full build
- `CAL_COM_API_KEY` and booking link/event-type (user to provide, as stated).
- `OPENROUTER_API_KEY` (user to provide, as stated).
- Meta Business Manager template approval for follow-ups (user action, no API key needed but requires manual submission/approval time — flag this has unpredictable turnaround and shouldn't block the rest of the build).

---

## Part 4 — Retrieval architecture: lightweight vector RAG (added 2026-09-21)

### 4.1 Why the plan changed
The catalog is much larger than initially scoped. The sitemap (`www.petrobindglobal.com/sitemap.xml`) lists **~72 real pages**: 8 standard Bitumen grades, 19 Oxidized Bitumen grades, 21 Bitumen Emulsion grades, Polymer-Modified Bitumen, plus 12 resource/FAQ articles (pricing explained, storage/handling, grade-by-application guide, bitumen vs asphalt, how to read a COA, Malaysia paving standards, etc). This is too much to hand-curate into one static Python dict (Part 1/2's original `knowledge_base.py` approach) and too much to stuff into every system prompt call.

**Decision: lightweight vector RAG, not GraphRAG.** GraphRAG earns its cost when answers require multi-hop relational reasoning across linked entities (e.g. "which grade pairs with which emulsion under which regional standard"). Petrobind's pages are mostly self-contained per grade/topic — a question about "Bitumen 60/70" needs that one page's content, not a graph traversal. Plain vector retrieval (embed chunks, cosine-similarity search at query time, each chunk tagged with its source URL) solves the actual problem — accurate, page-scoped answers with a correct link back to source — without graph-construction overhead.

### 4.2 Content acquisition (supersedes Part 1/3.1's manual curation)
The site appears to be client-side rendered (a plain HTTP fetch of `/products` returned only a header, not the product body) — so scraping must use a **headless browser**, not a bare `httpx` GET. This is a one-time, offline build step (not part of the running service):
- `scripts/scrape_site.py`: for each of the ~72 sitemap URLs, render with a headless browser (Playwright, added as a **dev-only** dependency, not in `requirements.txt` used by the deployed service) and extract clean page text (title, headings, spec tables, body copy).
- Save raw scraped pages to `rag/raw_pages/<slug>.json` (`{url, title, text}`) — committed to the repo so re-scraping isn't required unless the site changes, and so a human can spot-check pages before they're embedded.
- Flag explicitly: a Petrobind team member should spot-check at least the Bitumen grade pages and the pricing/COA resource articles for accuracy before go-live, same review requirement as before — just applied to scraped raw content instead of a hand-written dict.

### 4.3 Building the vector index (one-time / re-run on content change)
- `scripts/build_index.py`: chunk each page's text (e.g. ~500-token chunks, page title + URL kept as metadata on every chunk), call an embeddings API once per chunk, and write the result to `rag/index.json` — a flat list of `{url, title, chunk_text, embedding}` — committed to the repo (small enough at this scale: ~72 pages × a few chunks each ≈ a few hundred vectors).
- **New credential needed**: OpenRouter does not serve an embeddings endpoint, so embeddings need a separate provider — recommend OpenAI's `text-embedding-3-small` (cheap, well-supported) via a new `OPENAI_API_KEY` env var, used *only* for embeddings; chat completions stay on OpenRouter as already decided. (If the user would rather avoid a second provider, a keyword/BM25 fallback without embeddings was the alternative — flagged but not chosen.)
- Re-run this script (and `scrape_site.py` before it) whenever product/resource content changes on the live site; the resulting `rag/index.json` is what the deployed service actually reads — there is no live scraping at request time.

### 4.4 Runtime retrieval (`rag/retrieve.py`)
- At service startup, load `rag/index.json` into memory (list of vectors — small enough for plain Python/numpy, no external vector database needed at ~72-page scale).
- `retrieve(query: str, top_k: int = 4) -> List[RetrievedChunk]`: embeds the incoming buyer question (one embeddings API call), computes cosine similarity against the loaded vectors, returns the top-k chunks above a minimum similarity threshold (chunks below threshold are dropped rather than forced in — if nothing matches well, retrieval legitimately returns empty).
- `llm_assistant.py`'s turn handler (Part 2/3) calls `retrieve()` on each incoming buyer message and injects the returned chunks (with their URLs) into that turn's context as "reference material for this question," rather than embedding the entire catalog in the static system prompt. The static system prompt keeps only: company identity, tone rules, the zero-hallucination rule, and tool-use instructions (Part 3.4/3.5) — none of the per-product facts.
- Zero-hallucination rule updated accordingly: **"Only state product/spec facts that appear in the reference material provided for this turn. If retrieval returns no relevant chunk, or the chunk doesn't fully answer the question, call `request_sales_handoff` — do not answer from general knowledge."** This is a tighter, more defensible version of Part 3.5's rule now that "the knowledge base" is dynamic-per-turn rather than one fixed block.
- Every product link the assistant shares is the `url` field of whichever retrieved chunk it drew the answer from — this replaces the manually-maintained `product_url` field from Part 3.1 with something that scales to all ~60 product pages automatically.
- **Deterministic gate, not prompt-only**: when `retrieve()` returns zero chunks above threshold for a question that looks product/spec-related, `handle_incoming_message` short-circuits in code straight to the `request_sales_handoff` path (skipping a free-form model answer entirely) rather than relying on the model to choose correctly every time. This removes the riskiest decision from the model's discretion regardless of which model is running — see Part 5.2.

### 4.5 Updated file list (supersedes 3.7's `knowledge_base.py` entry)
```
scripts/scrape_site.py    new, dev-only — one-time headless-browser scrape of all sitemap URLs → rag/raw_pages/*.json
scripts/build_index.py    new, dev-only — chunk + embed raw pages → rag/index.json
rag/raw_pages/*.json       new — human-reviewable scraped page content (committed)
rag/index.json             new — embedded chunks + metadata (committed)
rag/retrieve.py            new — startup load + cosine-similarity retrieval, used at request time
llm_assistant.py           modified from Part 2/3 — calls retrieve() per turn instead of embedding a static KB; system prompt trimmed to identity/tone/guardrails/tools only
```
(`knowledge_base.py` from Part 1/2/3 is dropped — replaced by the `rag/` pipeline above. Company profile/tone copy that isn't product-specific can stay as a small constant in `llm_assistant.py` or a tiny `company_profile.py`.)

### 4.6 New/updated env vars
Adds `OPENAI_API_KEY` (embeddings only) to the list already in 3.8.

### 4.7 Testing additions
Extend Part 3.6's persona testing to include at least one question per product *category* (standard Bitumen, Oxidized Bitumen, Bitumen Emulsion, Polymer-Modified Bitumen) to confirm retrieval finds the right page and the correct URL comes back, plus one deliberately obscure/unlisted product question to confirm empty retrieval correctly triggers `request_sales_handoff` instead of a guess.

---

## Part 5 — Model/cost decision and conversational steering (added 2026-09-21)

### 5.1 Model decision: `openai/gpt-5-nano`
At the user's expected volume (~100,000 WhatsApp messages/day ≈ 6B input + 600M output tokens/month at a rough ~2,000 in / ~200 out tokens-per-turn estimate), model price differs by up to ~70x across tiers. Live OpenRouter pricing pulled 2026-09-21:

| Model | Prompt $/M | Completion $/M | Est. cost at 100K msg/day |
|---|---|---|---|
| `mistralai/mistral-nemo` | $0.019 | $0.03 | ~$132/mo |
| `deepseek/deepseek-v4-flash` | $0.036 | $0.071 | ~$259/mo |
| **`openai/gpt-5-nano` (chosen)** | $0.050 | $0.400 | **~$540/mo** |
| `google/gemini-2.5-flash-lite` | $0.100 | $0.400 | ~$840/mo |
| `anthropic/claude-haiku-4.5` | $1.000 | $5.000 | ~$9,000/mo |

**Decision: default `OPENROUTER_MODEL=openai/gpt-5-nano`.** Reasoning: it's the cheapest model in the "reliable, published tool-calling benchmarks" bracket (OpenAI/Anthropic/Google), landing close to open-weight pricing while carrying materially better documented instruction-following/tool-calling reliability than `mistral-nemo`/`deepseek-v4-flash` for a brand-sensitive B2B use case. Cheaper open-weight models remain a documented fallback (Part 3.10) if cost pressure increases and persona testing (Part 3.6) validates them first — never swapped in without re-running that suite.

**RAG as the primary cost lever, not model choice**: the Part 4 vector-RAG architecture is what keeps this affordable at all — injecting only ~4 relevant chunks (~500-800 tokens) per turn instead of the full ~72-page catalog cuts input tokens by roughly 10-20x versus a static-full-catalog prompt. Model tier is the second-order cost lever on top of that.

**Caveat on the cost table above**: these are estimates from an assumed average tokens-per-turn, not a guarantee — actual spend should be monitored against OpenRouter's usage dashboard in the first weeks of production traffic and the model/prompt-size tuned from real data.

**Infra note tied to volume**: 100K messages/day is large enough that the in-memory `conversation_store.py` (Part 2.2) and Render's default single-instance setup should be revisited before full launch — confirm expected concurrent-session count and whether a real datastore (Redis/Postgres) and a scaled-up Render plan are needed, rather than assuming the v1 in-memory design holds at this volume.

### 5.2 Conversational steering — keep chats on-track toward a purchase decision
New system-prompt behavior, since buyers may ask many exploratory/tangential questions before (or instead of) moving toward a quote: the assistant should answer helpfully but **actively steer the conversation back toward a purchasing decision** rather than letting it wander indefinitely. Concrete instructions to add to the system prompt (`llm_assistant.py`, alongside Part 3.4/3.5):
- After answering a buyer's question, if no inquiry details have been captured yet, proactively ask one qualifying/next-step question (e.g. "Which grade were you looking at, and roughly what volume?") rather than just waiting for the buyer to lead.
- Cap free-form Q&A: once the assistant has answered **2-3 questions** in a row without any movement toward inquiry details, it should pivot explicitly — offer a quote request, the Cal.com booking link (Part 3.2), or hand off to sales — rather than continuing to answer an unlimited stream of questions. This mirrors the general product feedback pattern of "reduce back-and-forth, ask only 2-3 questions before recommending" (tracked in `TESTING.md`, Part 3.6).
- This is a tone/pacing instruction, not a hard turn-counter in code for v1 — validated via the persona tests (especially the "sophisticated trader" and "vague/low-intent browser" personas, which are most likely to over-explore) and tightened into an explicit code-side turn counter later only if prompt-only steering proves unreliable in testing.
