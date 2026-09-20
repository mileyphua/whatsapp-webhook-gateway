# Real-Customer Readiness GAPs — WhatsApp AI Assistant (v1)

> To answer your question: **"What did I miss to make this agent deal with real customers?"**
>
> The 8 files + RAG we scaffolded are enough for a **working V1 pilot** (5–20 buyers,
> human-in-the-loop via lead/handoff emails, no auto-follow-up via templates yet).
>
> But to run against real paying B2B petroleum buyers with confidence, you are
> missing the items below. Each item is grouped by priority:
> - **P0 (do BEFORE you go live with real buyers)** — legal/compliance, reliability, brand-safety
> - **P1 (do within first 2 weeks of pilot)** — conversion lift + reduced ops load
> - **P2 (nice to have after first 50 conversations)** — polish + scale

---

## P0 — Must-do before real buyers (6 items)

### P0.1 — 24-hour customer-support window + message templates for late follow-ups

**What's missing today:** The current assistant sends only free-form `type: text`
replies. Meta's policy is strict:
> Any reply sent >24 hours after the buyer's last inbound message MUST use a
> **pre-approved WhatsApp message template**. Freeform text replies outside the
> window fail with error `#131030 (Re-engagement message)`.

**Why this matters for Petrobind:** Real B2B traders take 2–5 days between
exchanges ("I need to check with the factory and revert"). Our current
/followups-scan cron fires nudges via `send_whatsapp_text()`, which sends
**freeform text**. If the nudge fires outside the 24h window, WhatsApp **drops
it silently** (or logs an error) and Petrobind loses that lead.

**Action — 3 concrete steps:**

1.  **In Meta Business → WhatsApp Manager → Templates** create these 2 templates
    (submit for review — typically auto-approved within 1–5 minutes when
    category = MARKETING with the exact opt-out footer below):

    > ⚠️ **DO NOT pick UTILITY (you'll get the "Category does not match" error).**
    > Meta defines UTILITY = messages tied to an EXISTING CONFIRMED
    > order/appointment/shipment/payment. A booking-link that was never clicked,
    > or an inquiry we're re-engaging on, is outbound re-engagement → **MARKETING
    > is the correct and ONLY approvable category.** You MUST also include the
    > `STOP to opt out` footer; Meta auto-flags Marketing templates without it.

    **Template A — *Inquiry follow-up*** (Category: **MARKETING** · Language: English)
    > Template name (use EXACTLY this in code later): `petrobind_inquiry_followup_v1`
    >
    > Body:
    > ```
    > Hi {{1}} — this is Petrobind Global following up on your {{2}} inquiry.
    > We had {{3}} as the target destination/Incoterms. We can lock in a firm
    > quote once you confirm the remaining details. Reply with updates here or
    > book a 30-min trading consultation at {{4}}.
    >
    > Reply STOP to opt out.
    > — Petrobind Sales Desk
    > ```
    > Parameters: 1=first_name 2=product_name 3=destination_or_incoterms 4=booking_link

    **Template B — *Booking nudge*** (Category: **MARKETING** · Language: English)
    > Template name (use EXACTLY this in code later): `petrobind_booking_nudge_v1`
    >
    > Body:
    > ```
    > Hi {{1}} — Petrobind checking in. We shared a booking link for your
    > {{2}} discussion but no slot was confirmed yet. Pick a convenient
    > 30-min time at {{3}} or reply with your preferred day/time and we'll
    > adjust the calendar.
    >
    > Reply STOP to opt out.
    > — Petrobind
    > ```
    > Parameters: 1=first_name 2=product_or_topic 3=booking_link

    *Meta Marketing template rules these satisfy:* no misleading claims,
    opt-out footer present in last body section, body < 1024 chars,
    placeholders {{1}}–{{4}} typed as `text` type.

    If you still see a warning after picking MARKETING + adding the STOP line,
    click **Submit anyway / Request review** and note:
    > *"This is a legitimate follow-up to an inbound B2B trade inquiry the
    > recipient initiated on WhatsApp. Re-engagement is time-sensitive for
    > logistics pricing."* → Meta human review typically approves in hours.

2.  **Add `send_whatsapp_template(name: str, params: list[str])` helper to
    main.py** — identical payload shape to `send_whatsapp_text` but with
    `"type": "template"` and
    `"template": {"name": "...", "language": {"policy": "deterministic", "code": "en"}, "components":[{"type":"body","parameters":[{"type":"text","text":"..."}, ...]}]}`.

3.  **Modify /followups-scan endpoint to route through the template when
    >24h since the buyer's last inbound message.** The `last_activity_ts` on
    ConversationSession is the timestamp of their last message already. Rule of
    thumb: `(now - last_activity_ts) > 23h → send template. Else send freeform.`

---

### P0.2 — Handle NON-text inbound messages (image, document, audio, sticker, location)

**What's missing today:** In the webhook handler, we only extract `body` when
`message.type == "text"`. For any other message type from a buyer:
- `inbound_text` is `None`
- the LLM gets `""` as user input
- (graceful fallback path) → canned "which product are you interested in?" reply

**Why it matters for petroleum buyers:**
- Buyers send *PDF COA/PDS spec sheets* via WhatsApp for comparison.
- Buyers send photos of drums/flexitanks on arrival for QC claims.
- Buyers send 20-second voice notes saying "can I call you now?"

**Action — minimal viable implementation (1 afternoon):**

1.  In `_process_message` / receive_webhook, branch on `message.type`:
    - `type == "document"` → reply with:
      > *"Thanks — I've received your document and forwarded it to the trading
      > desk for review. A human will get back to you shortly with comments.
      > In the meantime, if this is a COA or quote comparison, which product
      > was it for and what destination port?"*
    - `type == "image"` → same as document (don't download / don't process yet).
    - `type == "audio"` / `type == "voice"` →
      > *"Got your voice note. I'll surface this to the trading desk right away.
      > If it's urgent, reply with the product + port and I can queue it."*
    - `type == "sticker"` / `type == "reaction"` → silently ignore (no reply).
    - `type == "location"` → same as document: capture + acknowledge + ask 1 clarifying Q.
2.  (Optional) Add `notify.send_whatsapp_media_alert(...)` email that includes
    the message ID + buyer number + file type to `SALES_EMAIL`, so a human sees
    the inbound media immediately in their inbox.

---

### P0.3 — Ingest Meta delivery/error statuses into session history so the cron/assistant knows what happened

**What's missing today:** `_process_status` prints a log line for every
sent/delivered/read/`failed` status, but does **not** write it to the
ConversationSession. We have `statuses` field subscribed on the Meta dashboard,
but nothing downstream reads it.

**Why it matters (real money):**
- WhatsApp rates on Render can occasionally fail with `#131026 (Recipient not
  valid)`, `#131047 (Spam rate limit)`, or transient network errors. Today we
  don't retry or notify.
- Read receipts (`status=read`) are a *very* strong signal: if the buyer read
  the inquiry nudge and didn't reply → next follow-up should be shorter + more
  direct ("Still around? 1-sentence update on target quantity or port, done.")
  instead of the same nudge twice.
- If a `BOOKING_CREATED` fires but the booking-confirmation WhatsApp message
  fails (e.g. 24h window closed), the buyer never hears the confirmation.
  Human sales desk must be paged.

**Action:**
- Append every status to session.history with synthetic `role: "meta"` rows
  (the LLM never sees them — caller filters them out before building the
  messages array). Use this data inside scan_for_followups() to:
  - Skip the nudge if the PREVIOUS nudge status is still `sent` (not delivered/read)
    within the last 2h (network delay).
  - If any `errors` on a prior nudge → fire `notify.send_handoff_email` with
    the error code so human tries different contact (e.g. calls the buyer).

---

### P0.4 — Explicit "human / speak to a person" intent routing (keyword + button reply)

**Missing today:** The model tries to detect off-topic/scheduling intent via
prompt engineering and calls `request_sales_handoff` + share_booking_link, but
for a buyer that literally types "human", "agent", "operator", "speak to a
person", "sales", "urgent" — you want an **instant, code-side, deterministic**
human-escalation path. No model in the loop. No LLM tokens spent. No risk.

**Why it matters:** 10–20% of buyers type "human" as their 2nd message.
Latency is critical (they type "human" when they're annoyed about a delay).
Running through the LLM adds 2–5s of latency *every time*.

**Action:**
In receive_webhook — BEFORE `handle_incoming_message`:
```python
HUMAN_KEYWORDS = ("human","person","agent","operator","real person",
                  "speak to someone","sales","urgent","escalate","manager")
if isinstance(inbound_text, str) and inbound_text.strip().lower() in HUMAN_KEYWORDS:
    send canned handoff:  "Of course — a Petrobind trading specialist will be
                          with you here directly within the next business hours.
                          For the fastest response, feel free to reply with the
                          product, port, and target quantity you're looking at
                          and they'll be fully briefed."
    fire request_sales_handoff email (if not already sent) with reason=HUMAN_ESCALATION_KEYWORD
    return 200
```

---

### P0.5 — Render service auto-restart + webhook reliability + duplicate payloads

**Two production incidents you want to avoid:**

1.  **Duplicate POST /webhook delivery.** Meta guarantees at-least-once
    delivery, not exactly-once. The same `wamid....` message ID can arrive
    twice 50 ms apart if the first ack was slow. Today → we run the full RAG
    + LLM twice and the buyer receives 2 identical replies (looks like a bug).
    → **Fix:** maintain a per-process `RecentMessageIDs = set()` bounded to
    last 10k message IDs; skip processing if the ID is already seen.
2.  **Render cold-start.** Render's free tier idles after 15min without HTTP
    calls. The first buyer message waits 60–120s during cold boot before the
    LLM reply is sent, or worse — Meta retries and you see duplicate behaviour.
    → **Fix 1:** Upgrade to Render Paid (Starter, $7/month) to disable idle
    sleeping. **Fix 2 (even if paid):** set up a 5-min uptime ping with
    https://cron-job.org to `GET /webhook` (empty — returns 200) so the
    instance stays hot.

---

### P0.6 — Hardened env + secrets validation at startup (fail loud, not silent)

Today's code gracefully handles missing env vars. For production you want the
opposite behaviour for **META auth tokens + WHATSAPP_PHONE_NUMBER_ID** — if
Render redeploys and one of them is a blank string, the webhook starts but
*every outbound WhatsApp message fails with RuntimeError*. The buyer sees
"delivered" and then… nothing.

**Action:** In `_startup_warm_index()` add:
```python
if not os.getenv("WHATSAPP_ACCESS_TOKEN") or not os.getenv("WHATSAPP_PHONE_NUMBER_ID"):
    raise RuntimeError("FATAL: WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required. Aborting Render startup. Check Render Environment tab.")
```

---

## P1 — High-impact in the first 2 weeks (5 items)

### P1.1 — WhatsApp "Quick Reply" buttons for the 2 most popular branching decisions

**Missing today:** After the assistant asks a yes/no style question (new vs
existing; want quote vs want call vs want handoff), the buyer types freeform
English which is noisy.

**Fix:** For the first turn of each conversation (and after each tool-based
handoff), send `type: "interactive"` with quick-reply buttons instead of
plain text:
- Buttons: `["Get a quote", "Book a call", "Talk to a human"]`
- After: `["Bitumen 60/70", "Bitumen 80/100", "Other grade"]`

Meta docs: `POST /{phone_id}/messages` → `"interactive"` payload.

---

### P1.2 — In-memory → Redis (or Postgres) session store for multi-instance Render / HA

**Problem:** `conversation_store._SESSIONS` lives in-process. If Render does a
rolling redeploy (new env var, new git push, OOM) → **every session is lost**.
The assistant forgets what the buyer said 5 minutes ago.

**Fix (lowest effort for Petrobind's scale):**
- Add `redis = Redis(host=..., port=6379, db=0)` with `redis_client.setex(f"sess:{phone}", 30days, JSON.dumps(session_dict))` on every append, and `json.loads(redis_client.get(..))` in `get_session`.
- Render has a managed Redis option ("Redis by Upstash" add-on, ~$7/month for 1GB).
- Less than 200 lines of code diff.

---

### P1.3 — Multi-language LLM routing (Bahasa Melayu, Simplified Chinese, Bahasa Indonesia, Vietnamese)

Petrobind's buyers are spread across MY/SG/ID/VN. The V1 spec (English-only)
means a buyer writing in Bahasa Melayu gets a canned English fallback.

**Action:** 1-line check *before* LLM call:
```python
# Cheap heuristic (<1ms). For higher accuracy use fasttext langdetect.
# If non-English, append a 1-sentence line to the system prompt that says
# "Reply in the SAME language the buyer wrote (do NOT switch to English)."
```

---

### P1.4 — Real handoff-to-human (not just an email): WhatsApp Conversation Ownership Transfer

Meta's feature: you can mark a conversation **"assigned to inbox"** so when a
human clicks the WhatsApp Manager → Shared Team Inbox they get auto-routed to
the exact conversation the AI had. Without this, the human receives the
handoff email and then has to **manually open WABA Manager, search for the
buyer phone, and type a new message** (which starts a brand-new conversation
thread — context is disconnected).

**Action:** Use the `POST /v22.0/{phone_number_id}/conversations` **Assign
conversation** endpoint. Docs: Meta WhatsApp Business Platform → "Assigning a
conversation". Adds ~20 lines of code.

---

### P1.5 — Quality log: store every (inbound, outbound, tool_calls, sims, reply) triple for prompt regression tests

**Problem:** Persona tests in TESTING.md are manual. The model regresses on a
prompt change ("why is the assistant quoting prices again?") and nobody notices
until a buyer complains.

**Action:** On every `_llm_reply`, append 1 JSON-line to
`logs/traffic/{YYYYMMDD}.jsonl`:
```json
{"ts": 178..., "phone": "+65...", "inbound_text":"...",
 "retrieved_hits":[{"title":"...","sim":0.719,"url":"..."}],
 "tool_calls":[{"name":"capture_trade_inquiry","args":"..."}],
 "outbound_reply":"...",
 "llm_latency_ms": 2831}
```
Keep 30 days locally → 1x/week ingest 50 random conversations into TESTING.md
as new dated Persona entries.

---

## P2 — Nice to have after first 50 real conversations (8 items)

| Priority | GAP |
|---|---|
| P2.1 | **WhatsApp Catalog (product inventory cards):** Host the 8 standard bitumen grades, PMB, Base Oil SN150, emulsions overview, oxidized overview as Meta Product Catalog cards. When a buyer types "grade options" send an `interactive → catalog` message instead of text. |
| P2.2 | **Catalog-driven tooling:** Add a `report_product_card_click(catalog_sku, phone_number)` webhook so if a buyer clicks the Bitumen 60/70 card → assistant immediately populates inquiry.product="Bitumen 60/70" and asks only the remaining 4 fields (saves 1 turn). |
| P2.3 | **PDF/COA download + auto Q&A:** Download inbound documents to a temp dir → OCR → add chunks to a temporary session-specific vector store → the assistant can answer "What is the penetration value on the COA you sent me?" directly. |
| P2.4 | **MOQ/Grade / Port availability live connector:** Replace the RAG (static) for availability with a real API call (Petrobind's ERP or a maintained Google Sheet). Updates daily. |
| P2.5 | **Rate-limits + WABA spam guard:** Track per-phone and global messages/15min. If a buyer sends >20 messages/min (spam), auto-block for 24h and notify. Prevents a $1000 OpenRouter bill from a bored script-kid. |
| P2.6 | **Citation / source-link footer on every LLM reply:** Append a single 1-line footer `[Source: petrobindglobal.com/products/bitumen-60-70]` if retrieval returned a hit. Boosts buyer trust. |
| P2.7 | **Part-owner / existing-partner CRM match:** When a buyer writes their company name in the 1st turn → HTTP-lookup against a Petrobind CRM/sheet (Google Sheets) → auto-set `is_known_partner=True` + pre-fill pricing-tier, account manager, usual ports. Reduces LLM qual questions by 2 turns. |
| P2.8 | **A/B test system-prompt variants behind a feature flag** (e.g. different opening greetings). Use P2.5 traffic log to compute Reply Rate per variant. |

---

## Summary — what to do TODAY before letting real buyers in

If you only have time for **3 things**, pick these and go live:
1.  **P0.2 (non-text media):** 5 lines of code — keep a buyer from thinking
    "Petrobind ignored my PDF".
2.  **P0.4 (human keyword):** 10 lines of code. 0 latency, no model involvement.
3.  **P0.6 (env validation):** Fail loud on Render deploy instead of silently
    dropping outbound messages.

The 24h template (P0.1) is the fourth to do by end of week. It's a manual
process with Meta (template submit → auto-approved in ~5 min).

Once the 7-persona tests pass against real deployed Render + your OPENROUTER key,
you are ready for a closed pilot of 5–10 real buyers with the rule: "the human
sales desk checks their email every 60 minutes during MY/SG business hours for
handoff/lead notifications."
