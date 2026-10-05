"""Per-phone-number conversation store, persisted to Upstash Redis.

Scope (per PLAN.md Part 2 / 3.3):
  - ONE ConversationSession per WhatsApp sender phone number (from-number).
  - Stores OpenAI-style chat history ({role, content}[], with tool_calls/tool_id),
    an InquiryDraft (partial quote-request data), booleans for existing partner
    vs new prospect, lead/handoff already-notified flag, and a follow-up tracker.
  - Backed by Upstash Redis (REST API, HTTPS) so sessions survive Render
    redeploys and idle-sleep restarts — previously (in-process-dict-only)
    the assistant "forgot" returning buyers and re-introduced itself every
    time the process restarted, which given free-tier idle-sleep and how
    often this app gets redeployed, was most of the time. Falls back to
    process-local memory only if UPSTASH_REDIS_REST_URL/TOKEN aren't set
    (e.g. local dev without a Redis account), same as before.
  - _SESSIONS is kept as an in-process read cache: within one process's
    lifetime, a session already loaded doesn't re-fetch from Redis on every
    field access, mutations happen on the in-memory object; save_session()
    is called at defined checkpoints (end of a turn) to flush it back.
  - History is trimmed to ~20 turns (roundtrip = 2) to bound token cost.

Known limitation: all_sessions() / scan_for_followups() / booking.py's
session lookup only see sessions already loaded into THIS process's
in-memory cache, not every session ever persisted to Redis. A session from
a buyer who hasn't messaged since before the last restart won't be picked
up by the idle-nudge cron or a Cal.com booking-confirmation match until
they message again (at which point get_session() lazy-loads it from
Redis). Scanning all Redis keys for these background jobs is future work.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

import httpx

MAX_HISTORY_TURNS = 20  # round-trips (each = user + assistant)
_REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
_REDIS_TTL_SECONDS = 60 * 24 * 3600  # 60 days — generous, well past OLD_SESSION_PRUNE_SEC


@dataclass
class InquiryDraft:
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    contact_position: Optional[str] = None  # job title / role at their company
    contact_email: Optional[str] = None
    product: Optional[str] = None
    quantity: Optional[str] = None
    destination_port: Optional[str] = None
    incoterm: Optional[str] = None
    packaging: Optional[str] = None
    additional_notes: Optional[str] = None
    is_new_prospect: Optional[bool] = None  # None = not yet asked

    def completeness_score(self) -> float:
        """0.0 = empty, 1.0 = all core fields filled (used to decide when to notify)."""
        vals = [
            self.company_name,
            self.contact_name,
            self.product,
            self.quantity,
            self.destination_port,
            self.incoterm,
            # contact_position, contact_email, packaging, notes are optional
            # (nice-to-have context) — don't penalize completeness for them.
        ]
        filled = sum(1 for v in vals if v)
        return round(filled / len(vals), 3)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "company_name": self.company_name,
            "contact_name": self.contact_name,
            "contact_position": self.contact_position,
            "contact_email": self.contact_email,
            "product": self.product,
            "quantity": self.quantity,
            "destination_port": self.destination_port,
            "incoterm": self.incoterm,
            "packaging": self.packaging,
            "additional_notes": self.additional_notes,
            "is_new_prospect": self.is_new_prospect,
            "completeness": self.completeness_score(),
        }


@dataclass
class ConversationSession:
    phone_number: str
    history: List[Dict[str, Any]] = field(default_factory=list)
    inquiry: InquiryDraft = field(default_factory=InquiryDraft)
    is_known_partner: Optional[bool] = None  # None = not asked yet
    last_activity_ts: float = field(default_factory=time.time)
    # --- Buyer memory (per PLAN: richer in-process personalization) ---
    first_seen_ts: float = field(default_factory=time.time)  # set once, at creation
    message_count: int = 0  # incremented per inbound buyer message (not assistant replies)
    lead_notified: bool = False  # prevent duplicate notification emails
    handoff_notified: bool = False
    followed_up_at: Optional[float] = None  # for Part 3.3 cron follow-ups
    freeform_questions_answered: int = 0  # Part 5.2 Q&A cap
    web_search_count: int = 0  # cost guardrail: cap search_industry_info calls per session
    booking_intent_notified: bool = False  # dedup: 1 "buyer wants to book" email per session

    # --- Booking / scheduling state (follow-up loop, see scan_for_followups) ---
    booking_link_shared_at: Optional[float] = None  # unix ts we shared the Cal link
    booking_confirmed_at: Optional[float] = None    # unix ts the /cal-webhook BOOKING_CREATED fired
    booking_details: Dict[str, Any] = field(default_factory=dict)  # raw Cal.com payload fields
    booking_followup_sent_at: Optional[float] = None  # dedup: only send 1 follow-up nudge

    # --- Human-handoff queue (shared inbox) ---
    # last_buyer_ts: when the buyer last wrote (WhatsApp's 24h reply window runs from it).
    # needs_human_since: set when the AI hands the chat to a human; cleared when a human replies/resolves.
    last_buyer_ts: Optional[float] = None
    needs_human_since: Optional[float] = None
    needs_human_reason: str = ""

    # --- Booking reminders: link sent -> 1 reminder after 2h -> (interested? 1 more) -> human; never after 24h ---
    booking_reminders_sent: int = 0
    booking_last_reminder_at: Optional[float] = None
    booking_intent: str = ""                 # interested | later | declined | unclear ("" = not asked/answered yet)
    booking_intent_at: Optional[float] = None
    booking_human_flagged_at: Optional[float] = None

    # --- Inquiry follow-up state (Part 3.3 idle-session nudge) ---
    inquiry_followup_sent_at: Optional[float] = None  # dedup: 1 quote-request nudge max

    def append(self, role: str, content: str, **extra) -> None:
        msg: Dict[str, Any] = {"role": role, "content": content}
        msg.update({k: v for k, v in extra.items() if v is not None})
        self.history.append(msg)
        self.last_activity_ts = time.time()
        if role == "user":
            self.message_count += 1
            self.last_buyer_ts = self.last_activity_ts

    def relationship_summary(self) -> str:
        """Human-readable buyer-memory line, e.g. '4th message, first contacted
        3 days ago'. Used both in the LLM's system prompt (so replies can
        naturally reference continuity) and in lead/handoff emails (so the
        sales team can see how warm/long-running this contact is at a glance)."""
        elapsed = max(0.0, time.time() - self.first_seen_ts)
        if elapsed < 3600:
            age = "just started"
        elif elapsed < 86400:
            hours = int(elapsed // 3600)
            age = f"first contacted {hours}h ago"
        else:
            days = int(elapsed // 86400)
            age = f"first contacted {days} day{'s' if days != 1 else ''} ago"
        ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(self.message_count, f"{self.message_count}th")
        return f"{ordinal} message from this buyer, {age}"

    def recent_transcript(self, max_turns: int = 6) -> str:
        """Plain-text buyer/assistant transcript excerpt (most recent N turns)
        for lead/handoff emails, so the sales team can read what actually
        happened without opening WhatsApp. Skips tool-call/tool-result rows."""
        lines = []
        for msg in self.history:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            speaker = "Buyer" if role == "user" else "Assistant"
            lines.append(f"{speaker}: {content}")
        if not lines:
            return "(no prior messages this session)"
        return "\n".join(lines[-max_turns:])
        # Trim: keep most-recent MAX_HISTORY_TURNS round-trips. Each turn is
        # (user + assistant) so we keep 2 * MAX items (plus tool rows if any).
        cap = MAX_HISTORY_TURNS * 4
        if len(self.history) > cap:
            # Don't drop the leading system prompt — callers prepend it outside
            # this module. We only trim appended user/assistant/tool rows.
            self.history = self.history[-cap:]


_SESSIONS: Dict[str, ConversationSession] = {}

_PROCESS_START_TS: float = time.time()
_last_redis_full_scan_ts: Optional[float] = None


def _redis_key(phone_number: str) -> str:
    return f"session:{phone_number}"


def _serialize(session: ConversationSession) -> str:
    return json.dumps(asdict(session))


def _deserialize(data: Dict[str, Any]) -> ConversationSession:
    inquiry_data = data.pop("inquiry", None) or {}
    inquiry_fields = {f.name for f in fields(InquiryDraft)}
    inquiry = InquiryDraft(**{k: v for k, v in inquiry_data.items() if k in inquiry_fields})
    session_fields = {f.name for f in fields(ConversationSession)}
    kwargs = {k: v for k, v in data.items() if k in session_fields}
    return ConversationSession(inquiry=inquiry, **kwargs)


async def _redis_load(phone_number: str) -> Optional[ConversationSession]:
    if not (_REDIS_URL and _REDIS_TOKEN):
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_REDIS_URL}/get/{_redis_key(phone_number)}",
                headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
            )
        resp.raise_for_status()
        raw = resp.json().get("result")
        if not raw:
            return None
        return _deserialize(json.loads(raw))
    except Exception as exc:  # pragma: no cover - best-effort, never block a reply
        print(f"[conversation_store] Redis load failed for {phone_number!r}: {exc!r}")
        return None


async def save_session(session: ConversationSession) -> None:
    """Flush a session's current state to Redis. Call at the end of a turn
    (after mutations are done), not after every individual field change —
    cheap enough to call generously, but doesn't need to be in the hot path
    of every attribute assignment. Best-effort: never raises, a save
    failure just means the NEXT successful save catches up the state.

    Additive (no breaking change): also mirrors a compact snapshot of the
    session to Supabase.sessions (for the shared-inbox UI's v_inbox_chat_list
    view). Supabase mirror runs in a fire-and-forget asyncio task — it is
    never awaited, never blocks the caller, never raises to the caller."""
    if _REDIS_URL and _REDIS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_REDIS_URL}/set/{_redis_key(session.phone_number)}",
                    headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                    params={"EX": str(_REDIS_TTL_SECONDS)},
                    content=_serialize(session),
                )
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - best-effort, never block a reply
            print(f"[conversation_store] Redis save failed for {session.phone_number!r}: {exc!r}")
    # --- Supabase mirror (additive, fire-and-forget) ---
    try:
        import asyncio as _aiom
        import supabase_client as _sc
        if not _sc.ENABLED:
            return
        _aiom.create_task(_sc.mirror_session(
            e164=session.phone_number,
            inquiry_dict=asdict(session.inquiry),
            history_list=list(session.history[-200:]),
            is_new_prospect=session.inquiry.is_new_prospect,
            lead_notified=bool(session.lead_notified),
            handoff_notified=bool(session.handoff_notified),
            booking_intent_notified=bool(session.booking_intent_notified),
            booking_link_shared_at_ts=session.booking_link_shared_at,
            followup_nudge_1_ts=session.booking_followup_sent_at,
            followup_nudge_2_ts=session.inquiry_followup_sent_at,
        ))
    except Exception as exc:  # pragma: no cover - mirror is a "nice to have"
        print(f"[conversation_store] Supabase mirror save skipped for {session.phone_number!r}: {type(exc).__name__}: {exc!s}")


async def get_session(phone_number: str) -> ConversationSession:
    """Get-or-create a session keyed by the raw WhatsApp 'from' number string.

    Checks the in-process cache first (cheap, no network), then Redis (a
    returning buyer whose session isn't cached in THIS process, e.g. after
    a redeploy or idle-sleep restart), then creates a fresh session if
    neither has one. Does NOT persist a freshly-created session by itself,
    callers save it via save_session() once it actually has state worth
    keeping."""
    if not phone_number:
        raise ValueError("phone_number is required")
    s = _SESSIONS.get(phone_number)
    if s is not None:
        return s
    s = await _redis_load(phone_number)
    if s is None:
        s = ConversationSession(phone_number=phone_number)
    _SESSIONS[phone_number] = s
    return s


def all_sessions() -> List[ConversationSession]:
    """Used by the follow-up cron job (Part 3.3) to scan idle sessions.
    Only sees sessions already loaded into this process's cache — see the
    module docstring's "Known limitation" note."""
    return list(_SESSIONS.values())


async def redis_scan_all_sessions(limit: int = 5000, *, force: bool = False) -> int:
    """Full Redis keyspace scan to hydrate this process's in-memory _SESSIONS
    cache with every persisted session (not just ones this process has seen
    via get_session since last restart).

    Safety guarantees:
    - 3-hour guard: skips re-scan if now()-_last_redis_full_scan_ts < 10800
      UNLESS _last_redis_full_scan_ts is None (never scanned) OR process
      started <10s ago (startup hydrate) OR force=True (admin override).
    - force=True (admin ops console): bypass the 3h guard AND the 10s
      post-startup guard.  Used by POST /api/inbox/admin/force-redis-scan so
      a human can re-hydrate immediately after a manual data repair on
      Redis keys without waiting 3 hours.
    - Wraps entire scan in try/except: Redis/network/parse failure prints
      ONE WARN line, returns 0 added, never raises (no crash).
    - Each individual key load is wrapped in its own try/except so one
      corrupt/partial row doesn't abort the rest of the scan.
    - Uses /scan cursor loop (Upstash REST API); falls back to
      GET /keys/session:* if /scan returns 404 (older/non-Upstash compat).

    Returns count of sessions newly added to _SESSIONS (not overwriting
    already-cached ones — those are presumed more recent)."""
    global _last_redis_full_scan_ts
    now = time.time()
    if not force and (
        _last_redis_full_scan_ts is not None
        and (now - _last_redis_full_scan_ts) < 10800
        and (now - _PROCESS_START_TS) >= 10
    ):
        return 0
    if not (_REDIS_URL and _REDIS_TOKEN):
        _last_redis_full_scan_ts = now
        return 0
    added = 0
    try:
        keys: List[str] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            cursor = "0"
            use_scan = True
            while True:
                if use_scan:
                    try:
                        resp = await client.get(
                            f"{_REDIS_URL}/scan",
                            headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                            params={"cursor": cursor, "match": "session:*", "count": "500"},
                        )
                        if resp.status_code == 404:
                            use_scan = False
                            continue
                        resp.raise_for_status()
                        data = resp.json().get("result", [])
                        if isinstance(data, list) and len(data) == 2:
                            next_cursor, batch_keys = data[0], data[1]
                        else:
                            next_cursor, batch_keys = "0", []
                        if isinstance(batch_keys, list):
                            keys.extend(batch_keys)
                        cursor = next_cursor
                        if cursor in (None, "0", 0) or len(keys) >= limit:
                            break
                    except Exception:
                        use_scan = False
                        continue
                else:
                    resp = await client.get(
                        f"{_REDIS_URL}/keys/session:*",
                        headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                    )
                    resp.raise_for_status()
                    raw = resp.json().get("result") or []
                    if isinstance(raw, list):
                        keys.extend(raw)
                    break
            if len(keys) > limit:
                keys = keys[:limit]
        for key in keys:
            try:
                if not isinstance(key, str) or not key.startswith("session:"):
                    continue
                phone = key[len("session:"):]
                if not phone or phone in _SESSIONS:
                    continue
                sess = await _redis_load(phone)
                if sess is not None:
                    _SESSIONS[phone] = sess
                    added += 1
            except Exception as row_exc:
                print(f"[conversation_store] redis_scan bad row {key!r}: {row_exc!r}")
    except Exception as exc:
        print(f"[conversation_store] WARN redis_scan_all_sessions failed: {exc!r}")
        return 0
    _last_redis_full_scan_ts = time.time()
    return added


async def reset_session(phone_number: str) -> None:
    """Test helper; also useful if buyer explicitly asks to restart."""
    _SESSIONS.pop(phone_number, None)
    if _REDIS_URL and _REDIS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{_REDIS_URL}/del/{_redis_key(phone_number)}",
                    headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                )
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"[conversation_store] Redis delete failed for {phone_number!r}: {exc!r}")


# ------------------- Follow-up / idle scanning (Part 3.3) -------------------
# These durations are intentionally conservative (once-per-nudge per session,
# and only on distinct triggers) to avoid WhatsApp spam that could get the
# WABA flagged. For real customers you can bump them down after seeing
# open-response rates in the trading desk's sales process.

BOOKING_NUDGE_DELAY_SEC = 20 * 3600  # 20h after sharing link, nudge once if not booked
INQUIRY_NUDGE_IDLE_SEC = 48 * 3600   # 48h idle + any inquiry progress, nudge once
OLD_SESSION_PRUNE_SEC = 30 * 24 * 3600  # drop sessions untouched for 30 days max


def _now() -> float:
    return time.time()


@dataclass
class FollowupAction:
    """Returned by scan_for_followups for each session that needs a nudge."""
    phone_number: str
    kind: str  # "booking_nudge" | "inquiry_nudge"
    message_text: str  # pre-built WhatsApp text to send (plain text, no MD)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phone_number": self.phone_number,
            "kind": self.kind,
            "message_text": self.message_text,
        }


def _booking_nudge_text(sess: ConversationSession) -> str:
    product = sess.inquiry.product or "your requested Petrobind product"
    return (
        f"Hi there — just a quick nudge re: the {product} enquiry. "
        f"We shared the booking link earlier but didn't see a confirmed slot yet. "
        f"Still want to lock in a 30-min call with the trading desk? "
        f"If that time doesn't work just reply with a rough day/time that works "
        f"for you and we'll adjust."
    )


def _inquiry_nudge_text(sess: ConversationSession) -> str:
    product = sess.inquiry.product or "your product enquiry"
    port = sess.inquiry.destination_port
    port_clause = f" to {port}" if port else ""
    return (
        f"Hi there — following up on {product}{port_clause}. "
        f"We can lock in a quote and formal COA/PDS once we have the remaining "
        f"details (quantity, packaging, and your target Incoterms work for a start). "
        f"Any update you can share would help me prepare the next steps for you."
    )


def prune_old_sessions() -> int:
    """Drop sessions untouched for 30+ days (bounded memory on long-lived processes).
    Returns number dropped. Safe to call every cron run."""
    cutoff = _now() - OLD_SESSION_PRUNE_SEC
    drop = [pn for pn, s in _SESSIONS.items() if s.last_activity_ts < cutoff]
    for pn in drop:
        _SESSIONS.pop(pn, None)
    return len(drop)


BOOKING_REMIND_AFTER_SEC = 2 * 3600       # first reminder this long after the link; also the wait before each next step
BOOKING_WINDOW_SEC = 24 * 3600             # no automatic reminder after this; a human takes over instead
BOOKING_MAX_REMINDERS = max(1, int(os.getenv("BOOKING_MAX_REMINDERS", "2") or 2))   # set 1 for strictly one reminder


def _booking_reminder_text(sess: ConversationSession, number: int) -> str:
    import booking  # local import: booking imports this module lazily
    name = (sess.inquiry.contact_name or "").strip().split(" ")[0] if sess.inquiry.contact_name else ""
    greet = f"Hi {name}" if name else "Hi"
    link = booking.get_booking_link(phone=sess.phone_number, name=sess.inquiry.contact_name or None, email=sess.inquiry.contact_email or None)
    if number <= 1:
        body = (f"{greet}, in case it got buried: here's the link to pick a time for a quick call whenever suits you. "
                f"No rush, and if another time works better just tell me.")
    else:
        body = (f"{greet}, one last nudge from me about the call. If picking a slot is a hassle, just tell me a day and time that "
                f"works and I'll ask a colleague to ring you instead.")
    return f"{body} {link}".strip() if link else body


def booking_followup_action(sess: ConversationSession, now: float) -> Optional["FollowupAction"]:
    """What (if anything) to do about a buyer who was sent the Cal.com link but has not booked.

    Never contact someone who booked or said no. One reminder `BOOKING_REMIND_AFTER_SEC` after the link; a second only
    if the buyer answered with interest, again after that wait; never more than BOOKING_MAX_REMINDERS and never after
    BOOKING_WINDOW_SEC. When the reminders are used up (or unanswered) the buyer goes to the human follow-up queue."""
    shared = sess.booking_link_shared_at
    if not shared or sess.booking_confirmed_at or sess.booking_human_flagged_at or sess.booking_intent == "declined":
        return None
    flag = FollowupAction(phone_number=sess.phone_number, kind="booking_human_flag", message_text="")
    if now - shared >= BOOKING_WINDOW_SEC:
        return flag
    n = sess.booking_reminders_sent
    if n == 0:
        if now - shared >= BOOKING_REMIND_AFTER_SEC:
            return FollowupAction(phone_number=sess.phone_number, kind="booking_reminder", message_text=_booking_reminder_text(sess, 1))
        return None
    last = sess.booking_last_reminder_at or 0.0
    answered = sess.booking_intent in ("interested", "later", "unclear") and (sess.booking_intent_at or 0.0) > last
    ref = max(last, sess.booking_intent_at or 0.0) if answered else last
    if now < ref + BOOKING_REMIND_AFTER_SEC:
        return None
    if answered and n < BOOKING_MAX_REMINDERS:
        return FollowupAction(phone_number=sess.phone_number, kind="booking_reminder", message_text=_booking_reminder_text(sess, n + 1))
    return flag


def scan_for_followups() -> List[FollowupAction]:
    """Idle-session scan — call via the /followups-scan HTTP endpoint from an
    external cron (e.g. cron-job.org, Render cron, or a local `while sleep 3600`).

    For each returned action, the caller (main.py endpoint) sends the
    message_text via WhatsApp Cloud API using the session's phone number.
    The caller also flips the matching dedup flag so a session is never
    re-nudged on the same trigger.
    """
    now = _now()
    out: List[FollowupAction] = []

    for sess in _SESSIONS.values():
        # Booking reminders / human hand-over (see booking_followup_action)
        booking_act = booking_followup_action(sess, now)
        if booking_act is not None:
            out.append(booking_act)
            continue
        if sess.booking_link_shared_at and not sess.booking_confirmed_at:
            continue   # booking flow owns this buyer (declined / waiting): no generic nudges on top

        # Inquiry nudge: inquiry has at least 1 field filled, not yet lead_notified,
        # idle >= INQUIRY_NUDGE_IDLE_SEC, AND no prior inquiry nudge.
        if (
            sess.inquiry.completeness_score() >= 0.1
            and not sess.lead_notified
            and not sess.inquiry_followup_sent_at
            and (now - sess.last_activity_ts) >= INQUIRY_NUDGE_IDLE_SEC
        ):
            out.append(FollowupAction(
                phone_number=sess.phone_number,
                kind="inquiry_nudge",
                message_text=_inquiry_nudge_text(sess),
            ))
            continue

    return out


async def mark_followup_sent(phone_number: str, kind: str) -> None:
    """Dedup flag flip — called by main.py after a nudge WhatsApp message is ACK'd.

    kind must be 'booking_nudge' or 'inquiry_nudge' (matches FollowupAction.kind)."""
    s = _SESSIONS.get(phone_number)
    if not s:
        return
    ts = _now()
    s.followed_up_at = ts
    if kind == "booking_reminder":
        s.booking_reminders_sent += 1
        s.booking_last_reminder_at = ts
        s.booking_followup_sent_at = ts
    elif kind == "booking_human_flag":
        s.booking_human_flagged_at = ts
        if not s.needs_human_since:
            s.needs_human_since = ts
            s.needs_human_reason = ("Booking follow-up: no call booked after reminders"
                                    + (f" (buyer's last answer: {s.booking_intent})" if s.booking_intent else " (buyer did not answer)"))
    elif kind == "booking_nudge":
        s.booking_followup_sent_at = ts
    elif kind == "inquiry_nudge":
        s.inquiry_followup_sent_at = ts
    await save_session(s)


__all__ = [
    "InquiryDraft",
    "ConversationSession",
    "get_session",
    "save_session",
    "all_sessions",
    "redis_scan_all_sessions",
    "reset_session",
    "MAX_HISTORY_TURNS",
    "FollowupAction",
    "scan_for_followups",
    "mark_followup_sent",
    "prune_old_sessions",
]
