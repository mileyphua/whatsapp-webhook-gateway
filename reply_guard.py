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
