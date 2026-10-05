# Petrobind WhatsApp — Architecture

Live, clickable version: `/inbox/architecture` (log in at `/inbox/login` first).

**Goal:** the AI (RAG + LLM) answers FAQs; a human can see every message in real time and take over any chat.

```mermaid
flowchart LR
  Buyer([Buyer on WhatsApp]) -->|message| Webhook[POST /webhook<br/>verify + dedupe]
  Webhook --> Gate{Human claimed<br/>this chat?}
  Webhook -.session.-> Redis[(Upstash Redis<br/>sessions)]
  Gate -->|no| RAG[RAG retrieve<br/>FAQ index]
  RAG -->|answer found| LLM[LLM gpt-5-mini]
  RAG -->|can't answer| Email[Email handoff]
  LLM --> Send[Send via Meta API]
  Send --> Buyer
  Gate -->|yes| Draft[AI draft suggestion<br/>not sent]
  Draft --> Inbox[Human Inbox UI<br/>/inbox/chats]
  Inbox -->|human reply| Send
  Gate -.mirror.-> Supa[(Supabase<br/>messages, claims)]
  Supa -.realtime.-> Inbox
  Cron[Follow-up cron] -.scan.-> Redis
```

## How it works
1. **Message in** — Meta posts to `/webhook` (`main.py`); signature verified, duplicates dropped.
2. **Who answers?** — if no operator holds the chat's claim, the AI replies. If one does, the AI stays silent and only offers a draft suggestion.
3. **RAG always runs** — retrieval happens on every message; if nothing relevant is found for a product question, the LLM is skipped and the team is emailed (zero-hallucination guardrail).
4. **Real-time inbox** — every buyer, AI and human message is mirrored to Supabase and pushed to `/inbox/chats` (Realtime, with an 8 s poll fallback).
5. **Human takeover** — Claim → type reply → sent through the same Meta send path. Release → the AI resumes.
6. **Follow-ups** — a scheduled job re-engages idle chats within buyer-local quiet hours; outside the 24 h window only approved templates are sent.

Supabase and Redis are optional/fire-and-forget: if either is down, WhatsApp replies still work.

## Platform map (in-app navigation)

| Page | URL | Purpose |
|---|---|---|
| Inbox | `/inbox/chats` | One row per number; search; ⋮ → name / delete; thread opens on the right; AI \| Human switch |
| Logs | `/inbox/logs` | Audit trail (logins, takeovers, replies, **chat deletions with transcript copy**, history imports) |
| Architecture | `/inbox/architecture` | This diagram as a clickable node graph; each node links to the page that shows it |
| Ops console | `/inbox/admin` | Live health, Redis, claims, schedules, template cache; **Import chat history** (Redis → Supabase) |
| Guide | `/inbox/guide` | Platform README: pages, how-tos, data stores, auto-generated API table |
| Health | `/health` | Dependency status JSON |

Delete flow: Inbox ⋮ → Delete chat → confirm → `DELETE /api/inbox/chats/{e164}` removes Supabase rows (messages, claim, session), the Redis session and the reference name, then writes a `chat_delete` event (who, when, transcript) to `audit_events`, visible in Logs.
