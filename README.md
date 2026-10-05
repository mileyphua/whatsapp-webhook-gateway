# Petrobind Global — WhatsApp Cloud API + RAG Shared Inbox

**Single project, single Render service, single Meta app** = Petrobind Global only.
NON-GOAL ❌: No multi-tenant SaaS code lives in this repo. The Agentic ISV Phase 2 project moved to its OWN standalone repository at ~/AI development/agentic-isv-saas — not tracked here. Historically a scaffold lived in a transitional placeholder folder within this repo; user will move that folder out and delete the placeholder once git init in the new repo succeeds.

---

## 1. Goal

Build a **shared Human + AI inbox** for the Petrobind WhatsApp number so that:

1. **AI (Jane Tan persona)** keeps auto-replying exactly as it does today — RAG over the 72-page
   Petrobind bitumen catalog, inquiry draft capture, booking-link share, handoff emails to sales —
   **ZERO behavior change** to the current WhatsApp buyer experience.
2. **Humans (Petrobind sales team)** can open a browser dashboard and:
   - See a list of all buyer chats (newest message first, unread bolded).
   - Click a chat → see the full WhatsApp thread (AI replies + buyer replies + any earlier
     human replies interleaved).
   - Type their own reply (text only at first) → send via the same Cloud API as the AI,
     timestamped in the thread.
   - See an **AI-suggested reply** pill under every chat (RAG-generated draft the AI *would*
     have sent if the human wasn't looking) → Accept (uses it verbatim), Edit (opens textarea
     pre-filled), or Discard (no-op, human writes their own).
   - Claim a chat so the AI **pauses replying automatically** while the human has the tab open
     (prevents double-sends). Closing the tab or inactivity 5 min releases the claim.
3. **Database**: Supabase Singapore project (separate from any SaaS projects later) replaces
   the current in-mem `_SESSIONS` / `_RECENT_WAMIDS` cache + Upstash Redis as the source of
   truth for messages, sessions, dedup IDs, and claims. Upstash is kept as a FALLBACK ONLY
   until Supabase smoke tests pass.

---

## 2. Architecture (Petrobind Global only, this repo)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Meta Cloud API                                  │
│   (Petrobind WABA, phone +60-xxxx, same app as today — NO CHANGES)       │
└───────────────┬──────────────────────────────────────▲───────────────────┘
                │ POST /webhook                        │ send_whatsapp_text()
                ▼                                      │
┌──────────────────────────────────────────────────────────────────────────┐
│                     Render Web Service (FastAPI, root/)                  │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ main.py (EXISTING, augmented with new /api/inbox/* routes)         │  │
│  │  ├─ OLD untouched routes: /health, /, /webhook GET/POST,           │  │
│  │  │     /cal-webhook, /followups-scan, /send-message                │  │
│  │  └─ NEW inbox routes (all require INBOX_ADMIN_TOKEN bearer):       │  │
│  │        GET  /api/inbox/chats                    → list chats       │  │
│  │        GET  /api/inbox/chats/{e164}/messages     → thread         │  │
│  │        POST /api/inbox/chats/{e164}/messages     → human send     │  │
│  │        POST /api/inbox/chats/{e164}/claim        → acquire mutex  │  │
│  │        DELETE /api/inbox/chats/{e164}/claim      → release mutex  │  │
│  │        GET  /api/inbox/chats/{e164}/suggestion   → AI draft       │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │ supabase_client.py (NEW)                                           │  │
│  │   ├─ async insert_inbound_message / insert_outbound_message       │  │
│  │   ├─ select_chats / select_messages / upsert_session              │  │
│  │   ├─ claim_acquire / claim_release / claim_is_human_held          │  │
│  │   └─ recent_wamid_seen / recent_wamid_mark                         │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │  Message persistence HOOK (non-breaking)                           │  │
│  │     → fire-and-forget asyncio.create_task() after every existing   │  │
│  │       _process_message() and after every successful                │  │
│  │       send_whatsapp_text() call. On hook failure, LOG ONLY, never  │  │
│  │       surface to the buyer — existing WhatsApp flow MUST survive   │  │
│  │       a Supabase outage / mis-typed env var.                       │  │
│  ├────────────────────────────────────────────────────────────────────┤  │
│  │  Claim mutex CHECK (non-breaking)                                  │  │
│  │     → inside _llm_reply() + _instant_handoff_reply(), BEFORE       │  │
│  │       actually sending: call claim_is_human_held(). If TRUE,       │  │
│  │       write message to DB only + print "[AI SKIP — human holds "   │  │
│  │       "claim] to=..." — buyer never sees a duplicate.              │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└───────────────┬──────────────────────────────────────▲───────────────────┘
                │                                      │
                ▼                                      │
┌──────────────────────────────────────────────────────────────────────────┐
│                    Supabase (Singapore, ap-southeast-1)                   │
│  Tables (0001_petrobind_shared_inbox.sql):                               │
│    messages(id, wamid, direction(buyer/ai/human), e164, text, ts, ...)  │
│    sessions(e164 PK, inquiry_jsonb, history_jsonb, lead_notified, ...)  │
│    recent_wamids(wamid PK, ts, 7d TTL index)                             │
│    inbox_claims(e164 PK, held_by, expires_at_ts, heartbeat_ts)           │
│    audit_events(id, actor, action, e164, detail_jsonb, ts)               │
│  RLS OFF (single-tenant). Enable Realtime on messages + inbox_claims.    │
└───────────────┬──────────────────────────────────────▲───────────────────┘
                │                                      │
                ▼                                      │
┌──────────────────────────────────────────────────────────────────────────┐
│  petrobind_frontend_app/ (NEW — HTMX + Jinja templates, SAME service)    │
│    GET  /inbox/                         → chat list page                 │
│    GET  /inbox/chat/{e164}              → thread page                    │
│  (static assets mounted at /inbox/static)                                │
└──────────────────────────────────────────────────────────────────────────┘

Design hard rules:
  1. Supabase env vars missing → /health still 200, /webhook still replies 200,
     AI still works 100% on Upstash/in-mem fallback. Log WARN, never crash.
  2. Existing WhatsApp behavior is GOLDEN. Any feature added to the inbox is
     ADDITIVE on top of the current 8 s debounce + RAG pipeline.
  3. One source of truth per data concern:
     - Inbound/Outbound message TEXT → Supabase.messages
     - Conversation state (inquiry, history, lead/handoff_notified) →
       Supabase.sessions (conversation_store.py still writes to in-mem +
       Upstash on every turn and ASYNCHRONOUSLY mirrors to Supabase;
       Supabase never read by llm_assistant.py until Week 3 cutover).
  4. AI draft suggestion pill in inbox UI = RERUN of llm_assistant.handle_incoming_message
     WITHOUT actually sending to WhatsApp (UI only flag passed through a new
     `draft_only=True` kwarg on handle_incoming_message → returns text, no
     side effects to conversation_store).
```

---

## 3. Env vars (incremental, no existing ones touched)

Add these to `.env.example` + the Render service. All NEW, all optional with
safe no-op defaults (Supabase persistence layer silently disables itself
without `SUPABASE_URL` set — no crashes).

```bash
# --- SHARED INBOX (Optional — if unset, inbox routes return 503, WhatsApp works as before)
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=ey...      # NOT the anon key — this is the server service role
INBOX_ADMIN_TOKEN=                  # long random string (python secrets.token_urlsafe(32))
                                    # Bearer auth for /api/inbox/* + /inbox/ pages.
INBOX_SESSION_EXPIRE_MINUTES=60     # login session cookie TTL for the web UI
```

---

## 4. Decision Gates (before any user-facing change)

Gate 1 (now, before any code merged):
- [ ] README + PETROBIND_SHARED_INBOX.md correct, no multi-tenant references left
- [ ] Root structure restored 100% to Petrobind-only (verified `main.py`,
      `llm_assistant.py`, `conversation_store.py` all in repo root).

Gate 2 (after Supabase schema + persistence hooks run):
- [ ] `curl /health` → 200 (no changes vs baseline)
- [ ] Simulate 1 webhook POST (curl w/ signed_request or skip HMAC for dev):
      → (a) WhatsApp AI reply actually sends (existing flow)
      → (b) 1 row inbound + 1 row outbound exist in Supabase.messages
      → (c) in-mem session + Upstash also written (no change from current)
- [ ] Simulate Supabase DOWN (wrong key):
      → (a) WhatsApp reply STILL sends 200 OK
      → (b) `[WARN] persistence hook failed: ...` single line in log, no stack trace, no retries

Gate 3 (after /api/inbox/* + claim mutex wired):
- [ ] curl w/ INBOX_ADMIN_TOKEN → GET /api/inbox/chats returns list len ≥1
- [ ] `POST /claim` then simulate new webhook → log "[AI SKIP — human holds claim]"
      and NO WhatsApp actually sent (screenshot-able proof that double-send is
      impossible).
- [ ] `DELETE /claim` then simulate → AI actually sends again.

Gate 4 (frontend HTMX inbox page):
- [ ] Login page at /inbox/login w/ INBOX_ADMIN_TOKEN form submit → cookie session 60 min
- [ ] Chat list shows newest first, unread (if any last_message_is_buyer) bold
- [ ] Thread page: message bubbles, AI pill suggestion with Accept/Edit/Discard

Gate 5 (staging Render deploy):
- [ ] Deploy on separate `petrobind-inbox-staging` Render service (clone of prod + Supabase +
      `INBOX_ADMIN_TOKEN` env) → point Meta App Webhook Test Sender at staging for 20
      messages, inbox renders everything, 0 regressions in flow vs prod.
- [ ] Then flip prod Render env SUPABASE_URL/KEY/TOKEN → redeploy → 20 messages live,
      inbox mirrors correctly, 0 buyer complaints → done.

---

## 5. File map being created / touched

**EXISTING files (small, non-breaking augmentations — see rule §2.2 and §2.3):**
- `main.py` — 4 route groups (existing routes untouched; add 6 inbox routes,
  2 persistence hook insertions, 2 claim-mutex checks before sends).
- `llm_assistant.py` — add `draft_only: bool = False` param + optional return
  that avoids session write / tool side effects (needed for UI suggestion pill).
- `conversation_store.py` — best-effort mirror write to Supabase.sessions row
  at end of every save_session(); read path still Upstash/in-mem.

**NEW files (all additive, no existing behavior changed by their existence):**
- `supabase_client.py` — async Supabase wrapper (httpx to rest URL, NOT the
  Supabase Python client — avoids version-pinning issues with current
  httpx 0.27.x).
- `PETROBIND_SHARED_INBOX.md` — this document.
- `supabase/migrations/0001_petrobind_shared_inbox.sql` — schema (messages,
  sessions, recent_wamids, inbox_claims, audit_events + indexes + triggers).
- `petrobind_frontend_app/__init__.py`
- `petrobind_frontend_app/routes.py` — Jinja template routes (login, chat list,
  thread).
- `petrobind_frontend_app/templates/base.html`
- `petrobind_frontend_app/templates/login.html`
- `petrobind_frontend_app/templates/chat_list.html`
- `petrobind_frontend_app/templates/chat_thread.html`
- `petrobind_frontend_app/static/app.css`
- `petrobind_frontend_app/static/app.js` — HTMX config + claim heartbeat every 15 s.

---

## 6. NOT in scope (for this repo / Petrobind Global)

- ❌ No multi-tenant, no embedded signup, no `whatsapp_business_management` scope.
- ❌ No Meta app changes on Petrobind's app (Tech Provider flag OFF on purpose).
- ❌ No Petrobind tokens cross-copied anywhere.
- ❌ No frontend framework heavier than HTMX 2.0 + Jinja. No React build step
  for Petrobind (later SaaS in new repo can use Next.js). Single Render service.
- ❌ No change to RAG retrieval, prompt, debounce window, or zero-hallucination
  guardrail in llm_assistant.py.
