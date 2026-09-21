# Petrobind WhatsApp Webhook Gateway

A WhatsApp Business Cloud API webhook that runs an AI sales assistant for
Petrobind Global (bitumen / base oil B2B trading). Buyers message the WABA
number, the assistant answers from a scraped knowledge base (RAG), captures
trade inquiries, books calls via Cal.com, and hands off to a human sales
director for pricing.

Main files:

| File | Responsibility |
|---|---|
| `main.py` | FastAPI webhook, message dedupe, message batching, WhatsApp send |
| `llm_assistant.py` | System prompt, tool-calling loop, guardrails |
| `rag/retrieve.py` | Vector retrieval over the scraped product catalog |
| `conversation_store.py` | Per-buyer session state, in-process only |
| `notify.py` | Lead/handoff email to the sales team |
| `booking.py` | Cal.com booking link + webhook |

---

## How RAG retrieval keeps this cheap and accurate

The knowledge base is 73 scraped pages (`rag/raw_pages/*.json`) chunked and
embedded into `rag/index.json` (282 chunks, `text-embedding-3-small`,
built by `scripts/build_index.py`).

**The model never sees the whole catalog.** On every buyer message,
`rag.retrieve()` (`rag/retrieve.py`) embeds just that message, does a cosine
similarity match against all 282 chunk vectors (a single numpy matmul,
sub-millisecond, no external vector DB needed at this scale — see the
module docstring), and returns only the top 4 chunks above a similarity
threshold (`MIN_SIMILARITY = 0.36`, `DEFAULT_TOP_K = 4`). Those 4 chunks
are what actually goes into the LLM's context for that turn
(`_format_references()` in `llm_assistant.py`).

**This is a per-turn search over the full catalog, not a fixed subset.**
If a buyer asks about Bitumen 60/70, then later asks about Base Oil SN150,
each message independently re-searches all 282 chunks and pulls back
whichever 4 are actually relevant to *that* question. Nothing is
"used up" — over a conversation, a buyer can reach any page in the
catalog, just filtered to what's relevant per message instead of the
model reading all 73 pages on every single reply.

Why not just send the whole catalog every time instead of the top 4?

- **Cost**: the full index is ~8.6MB. Injecting all 282 chunks into every
  message would multiply prompt tokens (and cost) by roughly 70x per
  message, even when the buyer only asked about one product.
- **Accuracy**: a narrow, relevant context is *more* reliable than a huge
  one, especially on a smaller model — stuffing in dozens of unrelated
  grades' specs raises the odds the model mixes up numbers between
  products, it doesn't reduce that risk.
- **Zero-hallucination guardrail**: if retrieval finds nothing relevant
  (empty result), the assistant is instructed to say so and hand off
  rather than guess from general knowledge — see `_format_references()`'s
  explicit "you MUST call request_sales_handoff" instruction when there
  are no hits, and the deterministic code-side handoff gate in
  `handle_incoming_message()` that bypasses the LLM entirely for
  zero-match product/spec questions.

## Other cost controls in `llm_assistant.py`

| Mechanism | What it does |
|---|---|
| `extra_body={"reasoning": {"effort": "low"}}` | Caps hidden reasoning-token spend on the OpenRouter call so tokens go to the actual reply, not invisible reasoning |
| `max_tokens=900` | Bounds worst-case reply length/cost per call |
| `MAX_HISTORY_TURNS = 20` (`conversation_store.py`) | Caps how much conversation history gets resent every turn |
| Prompt caching (automatic, OpenRouter/OpenAI) | The system prompt is static text sent every call — the provider caches repeated tokens at a lower rate. Measured: ~77% of prompt tokens were cache hits in a normal turn |
| Message batching (`main.py`, `DEBOUNCE_SECONDS = 8`) | 2-3 rapid WhatsApp messages from the same buyer get combined into ONE LLM call instead of one call per message |
| `booking_link_shared_at` dedup | Prevents re-offering the Cal.com link (and re-running that tool call) every single handoff in a conversation |
| `MAX_WEB_SEARCHES_PER_SESSION = 2` | `search_industry_info` (live web search, only for general non-Petrobind industry questions) is capped per conversation — RAG is always tried first and is explicitly instructed as the default, web search is the expensive last resort |

**Measured real cost** on `gpt-5-mini` (2026-09-22): a normal RAG-grounded
reply costs ≈ $0.001; a reply that also uses a web search costs about the
same (≈ $0.00096) — the OpenRouter web plugin didn't turn out to add much
overhead here, but it's still capped as a matter of not making it a habit.
