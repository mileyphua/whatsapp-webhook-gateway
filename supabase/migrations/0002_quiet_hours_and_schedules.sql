-- ============================================================
-- 0002_quiet_hours_and_schedules.sql
--
-- Additions for quiet-hours/weekend aware outbound scheduling.
-- Run AFTER 0001_petrobind_shared_inbox.sql.
--
-- New objects:
--   * enum  schedule_status    (pending, sent, failed, cancelled, claimed_temp)
--   * enum  schedule_direction (ai_nudge, human_send, template_send)
--   * table outbound_schedules (bigserial PK, e164 FK-ish, direction enum,
--                               scheduled_for timestamptz, status enum, etc.)
--   * 2 partial/btree indexes for the schedule claim-worker loop
--
-- Idempotent (create type if not exists via DO block; create table if not exists).
-- ============================================================

-- ---- Enums ----
do $$ begin
  create type schedule_status as enum (
    'pending', 'sent', 'failed', 'cancelled', 'claimed_temp'
  );
exception when duplicate_object then null; end $$;

do $$ begin
  create type schedule_direction as enum (
    'ai_nudge', 'human_send', 'template_send'
  );
exception when duplicate_object then null; end $$;

-- ---- Table: outbound_schedules ----
create table if not exists outbound_schedules (
    id               bigserial primary key,
    e164             text not null,
    direction        schedule_direction not null,
    template_name    text,
    template_params  jsonb not null default '{}'::jsonb,
    plain_text       text,
    sender_direction text default 'ai',
    created_by       text,
    scheduled_for    timestamptz not null,
    status           schedule_status not null default 'pending',
    sent_at          timestamptz,
    error_detail     text,
    created_at       timestamptz not null default now(),
    claim_session_id text
);

-- ---- Indexes ----
-- (1) The claim-worker hot path: find pending rows with scheduled_for <= now().
--     Cover both 'pending' and the transient 'claimed_temp' state so a stuck
--     row (process died mid-send) is visible to debugging queries.
create index if not exists outbound_schedules_pending_idx
    on outbound_schedules (status, scheduled_for)
 where status in ('pending', 'claimed_temp');

-- (2) Per-e164 lookups: "show all scheduled/sent nudges for this buyer" in the UI.
create index if not exists outbound_schedules_e164_idx
    on outbound_schedules (e164, created_at desc);

-- ---- Realtime enablement (uncomment AFTER confirming RLS = OFF for this table) ----
-- alter publication supabase_realtime add table outbound_schedules;
