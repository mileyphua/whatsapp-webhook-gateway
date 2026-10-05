-- ============================================================
-- 0001_petrobind_shared_inbox.sql
--
-- Single-tenant Petrobind Global shared inbox schema.
-- SUPABASE SINGAPORE (ap-southeast-1) project ONLY.
--
-- NOT multi-tenant. No RLS (single project = one company's data).
-- Run this file in Supabase SQL Editor AFTER creating the project.
-- ============================================================

-- ---- Extensions ----
create extension if not exists pgcrypto;

-- ---- Enums ----
do $$ begin
  create type message_direction as enum ('buyer', 'ai', 'human', 'system');
exception when duplicate_object then null; end $$;

-- ---- Tables ----

create table if not exists recent_wamids (
    wamid            text primary key,
    seen_at          timestamptz not null default now()
);
create index if not exists recent_wamids_seen_at_idx
    on recent_wamids (seen_at);

create table if not exists sessions (
    e164                     text primary key,
    inquiry_jsonb            jsonb not null default '{}'::jsonb,
    history_jsonb            jsonb not null default '[]'::jsonb,
    is_new_prospect          boolean,
    lead_notified            boolean not null default false,
    handoff_notified         boolean not null default false,
    booking_intent_notified  boolean not null default false,
    booking_link_shared_at   timestamptz,
    last_message_at          timestamptz,
    last_buyer_message_at    timestamptz,
    last_ai_reply_at         timestamptz,
    followup_nudge_kind_1_at timestamptz,
    followup_nudge_kind_2_at timestamptz,
    updated_at               timestamptz not null default now(),
    created_at               timestamptz not null default now()
);
create index if not exists sessions_last_message_at_idx
    on sessions (last_message_at desc);
create index if not exists sessions_last_buyer_at_idx
    on sessions (last_buyer_message_at desc)
 where last_buyer_message_at is not null;

create table if not exists messages (
    id                 bigserial primary key,
    wamid              text,
    direction          message_direction not null,
    e164               text not null,
    reply_to_wamid     text,
    text               text,
    media_type         text,
    media_url          text,
    payload_jsonb      jsonb default '{}'::jsonb,
    meta_statuses_jsonb jsonb default '{}'::jsonb,
    sent_id_from_graph text,
    errored            boolean not null default false,
    error_detail       text,
    created_at         timestamptz not null default now()
);
create index if not exists messages_e164_created_idx
    on messages (e164, created_at desc);
create index if not exists messages_created_idx
    on messages (created_at desc);
create unique index if not exists messages_dedup_wamid_direction_idx
    on messages (wamid, direction)
 where wamid is not null;
create index if not exists messages_wamid_idx on messages (wamid)
 where wamid is not null;

create table if not exists inbox_claims (
    e164          text primary key,
    held_by       text not null,
    session_id    text not null,
    acquired_at   timestamptz not null default now(),
    heartbeat_ts  timestamptz not null default now(),
    expires_at_ts timestamptz not null
);
create index if not exists inbox_claims_expires_idx
    on inbox_claims (expires_at_ts);

create table if not exists audit_events (
    id           bigserial primary key,
    ts           timestamptz not null default now(),
    actor        text not null,
    action       text not null,
    e164         text,
    detail_jsonb jsonb default '{}'::jsonb
);
create index if not exists audit_events_ts_idx on audit_events (ts desc);
create index if not exists audit_events_actor_action_idx
    on audit_events (actor, action, ts desc);
create index if not exists audit_events_e164_idx on audit_events (e164, ts desc)
 where e164 is not null;

-- ---- Moddatetime triggers ----
create or replace function update_updated_at_column()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end; $$;

drop trigger if exists trg_sessions_updated_at on sessions;
create trigger trg_sessions_updated_at
before update on sessions
for each row execute function update_updated_at_column();

drop trigger if exists trg_sessions_touch_last_message on messages;
create or replace function touch_session_last_message()
returns trigger language plpgsql as $$
begin
    insert into sessions (e164, last_message_at, last_buyer_message_at, last_ai_reply_at)
    values (
        new.e164,
        new.created_at,
        case when new.direction = 'buyer' then new.created_at end,
        case when new.direction = 'ai'    then new.created_at end
    )
    on conflict (e164) do update
    set last_message_at       = greatest(coalesce(sessions.last_message_at, '-infinity'::timestamptz), new.created_at),
        last_buyer_message_at = case
            when new.direction = 'buyer'
            then greatest(coalesce(sessions.last_buyer_message_at, '-infinity'::timestamptz), new.created_at)
            else sessions.last_buyer_message_at end,
        last_ai_reply_at = case
            when new.direction = 'ai'
            then greatest(coalesce(sessions.last_ai_reply_at, '-infinity'::timestamptz), new.created_at)
            else sessions.last_ai_reply_at end;
    return new;
end; $$;
create trigger trg_sessions_touch_last_message
after insert on messages
for each row execute function touch_session_last_message();

-- ---- Convenience view for the chat list (UI) ----
drop view if exists v_inbox_chat_list;
create view v_inbox_chat_list as
select
    s.e164,
    s.inquiry_jsonb,
    s.lead_notified,
    s.handoff_notified,
    s.last_message_at,
    s.last_buyer_message_at,
    s.last_ai_reply_at,
    (m.direction is not distinct from 'buyer'::message_direction) as last_message_is_buyer,
    m.direction as last_direction,
    m.text as last_message_text,
    m.created_at as last_message_created_at,
    c.held_by as claim_held_by,
    c.expires_at_ts as claim_expires_at
from sessions s
left join lateral (
    select direction, text, created_at
    from messages
    where e164 = s.e164
    order by created_at desc
    limit 1
) m on true
left join inbox_claims c on c.e164 = s.e164 and c.expires_at_ts > now();

-- ---- Seed: nothing for single-tenant Petrobind. ----
-- (All data arrives via the persistence hook on live webhook traffic.)
--
-- When you want to test with zero seed data, the UI will render an empty
-- chat list with a "Waiting for first WhatsApp message..." banner.
-- That is correct Gate 2 state.

-- ---- Realtime enablement (run AFTER confirming RLS = OFF for these) ----
-- alter publication supabase_realtime add table messages;
-- alter publication supabase_realtime add table inbox_claims;
-- alter publication supabase_realtime add table sessions;
