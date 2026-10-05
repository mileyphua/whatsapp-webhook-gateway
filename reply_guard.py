"""Deterministic reply guards (no LLM calls): stop the assistant repeating
itself and detect when a buyer explicitly asks for a human."""
import re
from difflib import SequenceMatcher
from typing import List

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_URL = re.compile(r"https?://|www\.", re.I)
_WORDS = re.compile(r"[a-z0-9']+")

# "talk to a person", "can someone call me", "I want a human", "connect me to your manager" ...
_HUMAN_ASK = re.compile(
    r"\b(?:"
    r"(?:speak|talk|chat|connect|transfer|put me)\s+(?:to|with|through to)\s+(?:a\s+|an\s+|your\s+|the\s+|real\s+)?"
    r"(?:human|person|people|someone|somebody|agent|representative|rep|manager|director|boss|owner|sales(?:\s+(?:team|person|director|rep))?|staff|colleague)"
    r"|(?:real|actual|live)\s+(?:human|person)"
    r"|(?:human|person|agent|manager)\s+(?:please|pls)"
    r"|(?:can|could|will|would)\s+(?:someone|somebody|anyone)\s+(?:call|phone|ring|contact|reply|respond|get back)"
    r"|(?:call|phone|ring)\s+me"
    r"|customer\s+(?:service|support)"
    r"|(?:i\s+)?(?:want|need|prefer|would like)\s+(?:a\s+|to\s+(?:speak|talk)\s+to\s+(?:a\s+)?)?(?:human|real person|person|manager|director)"
    r")\b",
    re.I,
)


def asks_for_human(text: str) -> bool:
    return bool(text and _HUMAN_ASK.search(text))


def _norm(s: str) -> str:
    return " ".join(_WORDS.findall(s.lower()))


def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]


def _similar(a: str, b: str, threshold: float) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


def repeated_sentences(new_text: str, previous_replies: List[str], *, min_words: int = 4, threshold: float = 0.86) -> List[str]:
    """Sentences of new_text that (nearly) repeat a sentence from an earlier reply."""
    prev: List[str] = []
    for p in previous_replies:
        prev.extend(s for s in split_sentences(p) if len(_WORDS.findall(s.lower())) >= min_words)
    out = []
    for s in split_sentences(new_text):
        if len(_WORDS.findall(s.lower())) < min_words:
            continue
        if any(_similar(s, p, threshold) for p in prev):
            out.append(s)
    return out


def strip_repeats(new_text: str, previous_replies: List[str], **kw) -> str:
    """Drop sentences already said earlier (keeps anything with a link). Never returns an empty reply."""
    dup = repeated_sentences(new_text, previous_replies, **kw)
    if not dup:
        return new_text
    keep_lines = []
    for line in new_text.split("\n"):
        sents = split_sentences(line)
        if not sents:
            keep_lines.append(line)
            continue
        kept = [s for s in sents if s not in dup or _URL.search(s)]
        if kept:
            keep_lines.append(" ".join(kept))
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(keep_lines)).strip()
    return result or new_text


def previous_assistant_replies(history: list, limit: int = 8) -> List[str]:
    out = [str(m.get("content")) for m in history if m.get("role") == "assistant" and m.get("content")]
    return out[-limit:]


# ------------------------------------------------------------------ order reminders
# A salesperson who keeps bringing up "your earlier order" sounds desperate. The AI chats casually and only talks
# about an earlier order when the buyer raises order topics themselves.
_ORDER_WORDS = re.compile(
    r"\b(?:orders?|ordered|ordering|quote|quotes|quotation|inquiry|inquiries|enquiry|enquiries|shipment|shipping|shipped|delivery|"
    r"deliver|invoice|invoices|po|purchase|payment|container|containers|loading|cargo|vessel|consignment|booking)\b", re.I)

_ORDER_REMINDER = re.compile(
    r"(?:"
    r"\byour\s+(?:earlier|previous|last|recent|pending|open|existing)\s+(?:order|inquiry|enquiry|quote|quotation|request|shipment)"
    r"|\b(?:following|follow(?:ing)?\s+up|circling|circle|checking|check(?:ing)?\s+in|touching|touch|any\s+update)\s+(?:up\s+)?(?:back\s+)?(?:on|about|regarding|with)\s+(?:your|the)\s+(?:\w+\s+){0,4}?(?:order|inquiry|enquiry|quote|quotation|request|shipment)"
    r"|\bas\s+(?:we\s+)?discussed\b"
    r"|\bthe\s+(?:order|inquiry|enquiry|quote|quotation)\s+(?:you|we)\s+(?:mentioned|discussed|placed|made|talked)"
    r"|\byou\s+(?:mentioned|asked\s+about|inquired\s+about|enquired\s+about)\s+(?:earlier|before|previously|last\s+time)"
    r"|\bregarding\s+your\s+(?:\w+\s+){0,3}?(?:order|inquiry|enquiry|quote|quotation)"
    r")", re.I)


def buyer_mentions_order(text: str) -> bool:
    return bool(text and _ORDER_WORDS.search(text))


def strip_order_reminders(reply: str, buyer_text: str) -> str:
    """Remove sentences that remind the buyer of an earlier order/inquiry when the buyer did not bring orders up.
    Never returns an empty reply."""
    if buyer_mentions_order(buyer_text):
        return reply
    kept_lines = []
    for line in reply.split("\n"):
        sents = split_sentences(line)
        if not sents:
            kept_lines.append(line)
            continue
        kept = [x for x in sents if not _ORDER_REMINDER.search(x)]
        if kept:
            kept_lines.append(" ".join(kept))
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return result or reply


# ------------------------------------------------------------------ humanised sending
def humanize_parts(text: str, *, min_total: int = 100, max_parts: int = 3) -> List[str]:
    """Split a long, multi-paragraph reply into at most `max_parts` WhatsApp messages (like a person typing
    a few short messages). Short replies stay one message. Nothing is dropped or reordered; a paragraph that
    ends with ':' stays with the line it introduces, and very short paragraphs join the previous one."""
    t = (text or "").strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    if len(paras) <= 1 or len(t) < min_total:
        return [t]
    merged: List[str] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        while p.rstrip().endswith(":") and i + 1 < len(paras):
            i += 1
            p = p + "\n" + paras[i]
        if merged and len(p) < 30:
            merged[-1] = merged[-1] + "\n\n" + p
        else:
            merged.append(p)
        i += 1
    if len(merged) > max_parts:
        merged = merged[: max_parts - 1] + ["\n\n".join(merged[max_parts - 1:])]
    return merged


# ------------------------------------------------------------------ booking intent
_DECLINED = re.compile(r"\b(?:not\s+interested|no\s+thanks?|no\s+thank\s+you|(?:don'?t|do\s+not)\s+need|no\s+need|not\s+looking|"
                       r"already\s+(?:have|got)\s+(?:a\s+|an\s+)?(?:supplier|vendor)|please\s+stop|stop\s+(?:messaging|texting|contacting|sending)|"
                       r"unsubscribe|remove\s+me|leave\s+me\s+alone)\b", re.I)
_INTERESTED = re.compile(r"\b(?:yes|yeah|yep|sure|ok|okay|interested|send|link|available|slots?|what\s+times?|when\s+can|sounds\s+good|will\s+do|book(?:ing|ed)?)\b", re.I)
_LATER = re.compile(r"\b(?:later|next\s+(?:week|month)|tomorrow|tonight|busy|will\s+(?:check|call)|let\s+me\s+check|check\s+with|get\s+back\s+to\s+you|not\s+now|another\s+time|reschedule|not\s+free)\b", re.I)


def classify_booking_intent(text: str) -> str:
    """Cheap keyword read of a reply to the booking link: declined | interested | later | unclear."""
    t = text or ""
    if _DECLINED.search(t):
        return "declined"
    if _INTERESTED.search(t):
        return "interested"
    if _LATER.search(t):
        return "later"
    return "unclear"
