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

    # --- Inquiry follow-up state (Part 3.3 idle-session nudge) ---
    inquiry_followup_sent_at: Optional[float] = None  # dedup: 1 quote-request nudge max

    def append(self, role: str, content: str, **extra) -> None:
        msg: Dict[str, Any] = {"role": role, "content": content}
        msg.update({k: v for k, v in extra.items() if v is not None})
        self.history.append(msg)
        self.last_activity_ts = time.time()
        if role == "user":
            self.message_count += 1

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
    failure just means the NEXT successful save catches up the state."""
    if not (_REDIS_URL and _REDIS_TOKEN):
        return
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
        # Booking nudge: we shared the Cal.com link, but nothing booked AND it
        # has been >= BOOKING_NUDGE_DELAY_SEC, AND we haven't nudged the booking yet
        if (
            sess.booking_link_shared_at
            and not sess.booking_confirmed_at
            and not sess.booking_followup_sent_at
            and (now - sess.booking_link_shared_at) >= BOOKING_NUDGE_DELAY_SEC
        ):
            out.append(FollowupAction(
                phone_number=sess.phone_number,
                kind="booking_nudge",
                message_text=_booking_nudge_text(sess),
            ))
            continue

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
    if kind == "booking_nudge":
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
    "reset_session",
    "MAX_HISTORY_TURNS",
    "FollowupAction",
    "scan_for_followups",
    "mark_followup_sent",
    "prune_old_sessions",
]
