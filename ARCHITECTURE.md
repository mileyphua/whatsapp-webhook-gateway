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
| Team | `/inbox/team` | Admin adds team members (username + password). Members see only the Inbox and can reply; their name is on the lock and on their messages |
| Skills & learning | `/inbox/learning` | The AI's skills (when to use / what to do); rate replies (👍/👎 in a chat) → new or improved skills you approve; approval-rate trend |
| Logs | `/inbox/logs` | Audit trail (logins, takeovers, replies, **chat deletions with transcript copy**, history imports) |
| Architecture | `/inbox/architecture` | This diagram as a clickable node graph; each node links to the page that shows it |
| Ops console | `/inbox/admin` | Live health, Redis, claims, schedules, template cache; **Import chat history** (Redis → Supabase) |
| Guide | `/inbox/guide` | Platform README: pages, how-tos, data stores, auto-generated API table |
| Health | `/health` | Dependency status JSON |

Delete flow: Inbox ⋮ → Delete chat → confirm → `DELETE /api/inbox/chats/{e164}` removes Supabase rows (messages, claim, session), the Redis session and the reference name, then writes a `chat_delete` event (who, when, transcript) to `audit_events`, visible in Logs.

## How the assistant improves (skills)

The assistant works from a library of **skills**, modelled on Claude skills (`SKILL.md`): a skill's **description** says *when* it applies and is always visible to the planner; its **instructions** say *what to do and why* and are loaded into the reply prompt only when the skill applies (or always, for tone-and-habit skills marked "every reply"). Two skills are built in ("Sound like a person", "Hand over to a human"); the rest are learned from feedback or written by hand.

1. **Rate** – under each AI message: 👍, or 👎 with tags, a note and an optional better reply (`POST /api/inbox/feedback`, stored in Redis).
2. **Learn** – `learning.distill()` groups unprocessed ratings into skills: a new skill, or a *suggested revision* of an existing one (auto-runs after 5 new ratings, or press the button). Factual complaints become "Knowledge base to check" items and never enter a prompt.
3. **Approve** – a human approves new skills and revisions on `/inbox/learning` (or edits/writes skills there). Only approved skills are used.
4. **Plan, then reply** – `learning.plan_reply()` makes a short private plan (intent, which skills apply, needs-human, sentences to avoid); `learning.build_guidance()` loads only the chosen skills plus up to 3 approved example replies. An explicit request for a person (`reply_guard.asks_for_human`) loads the hand-over skill and flags the chat for the human queue.
5. **Don't repeat** – `reply_guard.strip_repeats()` removes sentences already sent earlier in the chat.

The model is not fine-tuned: improvement comes from approved skills and examples, measured by the approval rate. The ratings and "better replies" also form the dataset a future fine-tune would need.

## Accounts and permissions

- **One admin**: logs in with the `INBOX_ADMIN_TOKEN` password (username empty or `admin`). Sees every page.
- **Team members** (`users_store.py`, Redis hash `inbox_users`; salted PBKDF2 hashes): added by the admin on `/inbox/team`, log in with username + password, see **only the Inbox** and can read, reply, hold a chat, rate AI replies and rename numbers. Admin-only APIs return 403 for them (`_requires_admin`), admin pages redirect them to the Inbox.
- **Sessions** are signed cookies carrying `role` and `user`; a disabled/removed member is cut off immediately. Logins are throttled (5 failures per IP+account → 5 min).
- **Locks** (`inbox_claims`) belong to a person (`u:<username>`), show their display name, and the admin can force-release. Human messages store `sent_by` so the thread shows who wrote them.
