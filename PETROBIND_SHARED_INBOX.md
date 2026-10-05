# PETROBIND SHARED INBOX — Technical Deep-Dive

Owner: Petrobind Global (single-tenant, this repo only). Multi-tenant SaaS lives in a
SEPARATE standalone GitHub repo (agentic-isv-saas) — scaffold split out per README.md
standalone-repo instructions.

---

## 1. Rationale (Why add this to Petrobind NOW, before SaaS)

Petrobind is the **reference implementation** of the product. The shared inbox is the #1
feature both real-estate + ecommerce future tenants will ask for. Shipping it on Petrobind
first:
1. De-risks the claim-mutex pattern on a real WhatsApp number with real buyers.
2. Proves that adding Supabase persistence on top of the existing Upstash/in-mem store is
   truly additive and never takes the WhatsApp flow down (Gate 2's explicit downtime test).
3. Generates 1000+ real shared-inbox messages / transcripts we can use as evidence when
   submitting `whatsapp_business_management` scope to Meta for the SaaS app.

---

## 2. Claims / concurrency correctness

**Claim mutex is per (e164) buyer chat, not per agent.** Claim semantics:

| Scenario | Outcome |
|---|---|
| Human opens tab #1 for `+6012...` | Acquires claim for 120 s, heartbeat every 15 s → stays claimed. |
| Human opens same chat on tab #2 on SAME browser login session | Claim is idempotent (same `held_by` + `session_id`); no error. |
| Human #2 opens same chat on DIFFERENT login session | Gets 409 Conflict `{held_by: "Jane Tan", expires_in: 95s}`. UI shows red banner "Claimed by Jane — you can read but not send."; send button disabled. |
| Human closes tab or laptop lid / no heartbeat for ≥ 120 s | Claim expires by `expires_at_ts` column check; next AI message auto-flows. |
| Claim held for chat `A` | Claim does NOT affect any other chat B, C, D. AI still replies on B/C/D normally. |
| Buyer types while human has claim open | Buyer msg still delivered, persisted, thread live-updates via Supabase Realtime. Human sees it. AI does NOT auto-reply. |
| Human clicks "Suggestion" pill's **Accept** | Claim-held send → outbound marked direction=`human`, context.wamid = buyer's, no AI send. Human explicitly chose to send. |
| Human clicks **Send** on their own text | Same as Accept → outbound direction=`human`. |
| Buyer says "urgent" / escalation keyword | `_instant_handoff_reply()` runs FIRST as today → claim check inside → **if human holds claim**: writes to DB only, skips actual WhatsApp send so human can respond. **If no claim**: sends WhatsApp instant handoff + email exactly as today. No change to non-claimed buyer experience. |

**Claim correctness proof (2 properties):**

1. **Double-send freedom (safety):** Every outbound send path
   (`_llm_reply`, `_instant_handoff_reply`, `followups_scan` → `send_whatsapp_text`,
   `/api/inbox/chats/{e164}/messages` → `send_whatsapp_text`) runs the exact same
   `claim_is_human_held(e164)` check BEFORE calling Graph API:
   ```
   IF sender=AI AND held=human_claim → DB-only write + log SKIP + return 200 to caller
   IF sender=human AND held=other_human → HTTP 409 with holder info
   ```
   `followups_scan` is the one exception for nudges: if a buyer has been idle 72 h,
   nudge still sends even if claim held (because human left tab open by accident;
   buyer-idle 72 h = stale claim). This is an explicit carve-out.

2. **Liveness (no AI deadlock forever on a forgotten tab):**
   Expiry timestamp on every claim; heartbeat only extends, never immortal.
   `expires_at_ts < now() → claim gone.` Worst-case buyer-stuck-on-claim = 2 minutes
   after a human's last heartbeat (we don't even notice the buyer waiting because
   the AI is idle during an active human chat anyway).

---

## 3. Database schema (Supabase)

See `supabase/migrations/0001_petrobind_shared_inbox.sql`. Key columns:

**messages** — single messages table for all traffic directions:
- `direction` enum `['buyer','ai','human','system']`
- `wamid text` — for buyer this is Meta message.id; for AI/human outbound this is
  the `messages[0].id` Graph API returns on successful send; for system (AI-skip
  note) = NULL.
- `reply_to_wamid text` — for threaded replies. Indexed.
- `e164` — buyer phone, composite index with `created_at desc`.
- `text text`, `payload_jsonb` (raw media payload if non-text; preserved for future
  image rendering in inbox).
- `meta_statuses_jsonb` — sent/delivered/read status map (from /webhook statuses).
- `dedup_key` = `(wamid, direction)` where wamid not null → partial unique index to
  enforce exactly-once writes for Graph-message IDs.

**sessions** — Supabase copy of ConversationSession (mirrored from in-mem + Upstash,
not yet read from by llm_assistant until Week 3):
- `e164 text PRIMARY KEY`
- `inquiry_jsonb jsonb not null default '{}'::jsonb`
- `history_jsonb jsonb not null default '[]'::jsonb`
- `lead_notified boolean default false`, `handoff_notified boolean default false`,
  `booking_link_shared_at timestamptz`, `last_message_at timestamptz`,
  `updated_at timestamptz default now()`
- Trigger: `update_updated_at()` on row change.

**recent_wamids** — dedup for Meta webhook duplicate deliveries:
- `wamid text PRIMARY KEY`, `seen_at timestamptz default now()`
- Index on `seen_at` WHERE `seen_at < now() - interval '7 days'` for cron-based prune.
  (Current in-process deque kept as fast-path; Supabase acts as 2nd-layer guardrail
  across Render restarts.)

**inbox_claims** — mutex table:
- `e164 text PRIMARY KEY`
- `held_by text not null` (human display name or login-id)
- `session_id text not null` (login cookie session id, so same-browser multi-tab is ok)
- `acquired_at timestamptz default now()`
- `heartbeat_ts timestamptz default now()`
- `expires_at_ts timestamptz not null` (always `heartbeat_ts + 120s`)
- Index on `expires_at_ts` for fast expiry scan.

**audit_events** — immutable append-only audit log:
- `id bigserial primary key`, `ts timestamptz default now()`
- `actor text not null` — `ai` / `human:<name>` / `system`
- `action text not null` — `send / claim_acquired / claim_released / login / ai_skipped_claim`
- `e164 text`, `detail_jsonb jsonb`
- No update/delete grants ever; INSERT only.

---

## 4. Persistence hook: "log only, never break WhatsApp"

In `main.py`, after existing code in two places we add ONE line each:

```python
# After _process_message(message) — for BUYER inbound:
asyncio.create_task(_persist_inbound_safe(message))

# After send_whatsapp_text() RETURNS successfully — for AI/human outbound:
asyncio.create_task(_persist_outbound_safe(to=from_number, sent_id=sent_id,
                                            text=reply_text, direction="ai",
                                            reply_to=reply_to_message_id))
```

Implementation of `_persist_inbound_safe`:
```python
async def _persist_inbound_safe(msg: Dict[str, Any]) -> None:
    if not _SUPABASE_ENABLED:  # SUPABASE_URL env missing
        return
    try:
        async with asyncio.timeout(2.0):      # 2 second ceiling, NEVER blocks WhatsApp
            await supabase_client.insert_inbound(msg)
    except Exception as exc:
        # SINGLE warning line, no stack trace, NO retry hammer (avoid cascading failures).
        print(f"[PERSIST-WARN] inbound {msg.get('id')!r} failed: {type(exc).__name__}: {str(exc)[:120]}")
```

**Downtime scenario analysis (Gate 2 — test this explicitly):**
- `SUPABASE_URL` = `https://does-not-resolve.example.co` → 2 s DNS timeout per
  hook → 1 warning line per inbound/outbound → WhatsApp sends on time.
- `SUPABASE_SERVICE_ROLE_KEY` = wrong → 401 from Supabase, same warning line
  pattern, no crash.
- Supabase 100 % down → 1 line per message, same. Buyer never notices.

---

## 5. AI suggestion pill (UI only, NO side effects)

The UI calls `GET /api/inbox/chats/{e164}/suggestion`. This runs the LLM pipeline
with two new guardrails so it cannot accidentally reply or mess with sessions:

1. `llm_assistant.handle_incoming_message(phone, text, draft_only=True)`:
   - Passes an internal `_draft_mode: bool = True` flag.
   - `conversation_store.get_session()` returns a **clone**, NOT the cached session,
     so no mutations leak.
   - After LLM returns text, it DOES NOT call `save_session()`, DOES NOT call
     `notify.send_handoff_email()`, DOES NOT call `notify.send_booking_interest_email()`.
   - Returns plain text only.
2. `/api/inbox/chats/{e164}/suggestion` caches the suggestion per `(e164,
   last_buyer_wamid)` for 300 s in an LRU in memory — don't burn LLM tokens every time
   a human refreshes the thread page.

---

## 6. Frontend deployment (same service)

Mount the Jinja-HTMX inbox at URL paths `/inbox/*` on the same FastAPI app. Same
Render service = same 5 ms network region between UI and /api/inbox/* endpoints.

Authentication:
- GET `/inbox/login` → HTML form → password field = `INBOX_ADMIN_TOKEN` (no user
  management, V1 single-admin password is fine for Petrobind 2-person sales team.
  Per-user login saved for SaaS project).
- Submit → set session cookie `inbox_session=<jwt>` signed with INBOX_ADMIN_TOKEN as
  key, TTL = `INBOX_SESSION_EXPIRE_MINUTES` (default 60). Cookie attributes:
  `HttpOnly, Secure (on Render https), SameSite=Lax, path=/inbox`. JWT carries
  `{"sub":"admin","name":"Petrobind Admin","sid":<random>,"iat":...,"exp":...}`.
- Every page under `/inbox/*` checks cookie signature. Invalid/expired → redirect
  302 to `/inbox/login`.
- Heartbeat + claim uses `sid` from JWT → claim table's session_id. So admin opening
  chat on 2 tabs (same login session) is idempotent claim-holder.
  Opening chat on a different login session (another computer with its own cookie)
  is a 409 Conflict and displays holder name.

Realtime updates:
- Messages & claims: Supabase Realtime subscribe (socket from browser → Supabase).
  On `INSERT` into messages, prepend bubble to thread without refresh.
  On claim RELEASE, UI re-enables send button / hides pill.

---

## 7. Non-goals / out of scope for V1

- ❌ No media rendering in inbox thread (image/audio/doc shows "📄 Attachment —
  open Meta Business Suite to view" placeholder; V2 later).
- ❌ No message editing / deletion (Meta supports this; V2 inbox).
- ❌ No per-agent queues / assignment rules; Petrobind team is 2 humans.
- ❌ No CSV export or analytics dashboard.
- ❌ No translation.
- ❌ No calendar or booking widget inside inbox (external link to cal.com still
  via AI / human adds the URL in their reply manually).
- ❌ No SSO / role-based auth. Single shared inbox admin token for Petrobind V1.
- ❌ No Grafana / Sentry dashboards integration for inbox (Sentry can be added
  env-only later; not a code task).

---

## 8. Smoke test script (run locally)

```bash
# Step 1: build venv + env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill .env with: WHATSAPP_APP_SECRET, WHATSAPP_ACCESS_TOKEN,
# WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, OPENROUTER_API_KEY,
# SUPABASE_URL (optional, leave unset to test DOWN-mode),
# SUPABASE_SERVICE_ROLE_KEY (optional), INBOX_ADMIN_TOKEN=dev-token-123

# Step 2: server
uvicorn main:app --reload --port 8000

# Step 3: health
curl -s http://localhost:8000/ | python3 -m json.tool

# Step 4: simulate buyer inbound (NO sig or WRONG sig to test DOWN-mode)
# With INBOX_ADMIN_TOKEN set → verify GET /api/inbox/chats returns 1 chat after
# you follow through with a Graph send via /send-message endpoint.
curl -X POST http://localhost:8000/send-message \
  -H 'Content-Type: application/json' \
  -d '{"to":"+6012TESTING","text":"Test outbound through gateway"}'

# Step 5: claim + verify AI skip
curl -X POST http://localhost:8000/api/inbox/chats/%2B6012TESTING/claim \
  -H 'Authorization: Bearer dev-token-123'
# Then curl another buyer message → logs should show [AI SKIP — human holds claim]
```
