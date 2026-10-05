"""Feedback-driven learning for the WhatsApp assistant.

The model itself is not fine-tuned. Instead:
  1. humans rate each AI reply (feedback_store)
  2. distill() turns new feedback into short behaviour lessons (status=pending)
  3. a human approves lessons; approved ones are injected into every reply
     prompt via guidance_block() together with a few good example replies
  4. plan_reply() thinks before answering (intent, human needed?, what to avoid)

Lessons are about HOW to reply (tone, structure, when to hand over). They never
carry product facts: facts come only from the RAG knowledge base.
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import feedback_store as fs

# Always-on writing rules (kept short: they are sent with every reply).
BASE_WRITING_RULES = """\
Writing like a person, not a template:
- Reply to what this buyer just said. Do not reuse a sentence, opener or closing line you already sent earlier in this chat; vary your wording.
- Do not restate facts or links you already gave unless the buyer asks again.
- No stock phrases ("Great question", "I hope this helps", "Feel free to reach out"), no repeated thank-yous, no filler.
- Keep it short and natural for WhatsApp: usually 1-3 short sentences. Answer first, then at most one useful follow-up question.
- If you cannot answer from the reference material, or the buyer asks for a person, say a colleague will pick it up here; do not guess."""

_cache: Dict[str, Any] = {"at": 0.0, "text": ""}
_CACHE_TTL = 60.0
MAX_LESSONS_IN_PROMPT = 15
MAX_EXAMPLES_IN_PROMPT = 3
AUTO_LEARN_AFTER = 5  # unprocessed ratings before a distill run is triggered


def invalidate_cache() -> None:
    _cache["at"] = 0.0


async def guidance_block() -> str:
    """System-prompt block: writing rules + approved lessons + good examples. Never raises."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["text"]
    parts = [BASE_WRITING_RULES]
    try:
        lessons = [l for l in await fs.list_lessons("active") if l.get("kind") != "knowledge"][:MAX_LESSONS_IN_PROMPT]
        if lessons:
            parts.append("Lessons from the sales team's feedback on past replies (follow these):\n" + "\n".join(f"- {l['text']}" for l in lessons))
        fb = await fs.list_feedback()
        examples: List[Tuple[str, str]] = []
        for i in fb:
            if i.get("rating") == "down" and i.get("better_reply") and i.get("buyer_text"):
                examples.append((i["buyer_text"], i["better_reply"]))
            elif i.get("rating") == "up" and i.get("buyer_text") and len(examples) < MAX_EXAMPLES_IN_PROMPT:
                examples.append((i["buyer_text"], i["ai_text"]))
            if len(examples) >= MAX_EXAMPLES_IN_PROMPT:
                break
        if examples:
            parts.append("Examples of replies the team approved (match their tone and brevity, do not copy the content):\n" +
                         "\n".join(f"Buyer: {b[:300]}\nGood reply: {g[:500]}" for b, g in examples))
    except Exception as exc:
        print(f"[learning] guidance_block failed: {type(exc).__name__}: {exc!s}")
    text = "\n\n".join(parts)
    _cache.update(at=now, text=text)
    return text


# ------------------------------------------------------------------ planner

def _json_from(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def _llm_json(system: str, user: str, *, max_tokens: int, timeout: float) -> Optional[dict]:
    import llm_assistant  # lazy: avoids a circular import at module load
    client = llm_assistant._openrouter_client()
    if client is None:
        return None
    model = os.getenv("OPENROUTER_MODEL") or llm_assistant.OPENROUTER_MODEL_DEFAULT
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1, max_tokens=max_tokens, timeout=timeout,
            extra_body={"reasoning": {"effort": "low"}},
        )
        return _json_from(resp.choices[0].message.content or "")
    except Exception as exc:
        print(f"[learning] llm json call failed: {type(exc).__name__}: {exc!s}")
        return None


_PLANNER_SYSTEM = """\
You plan a WhatsApp reply for Petrobind's sales assistant BEFORE it is written. Think about the buyer, do not write the reply.
Return ONLY JSON: {"intent": str, "answerable_from_references": bool, "needs_human": bool, "human_reason": str,
"points": [str], "avoid": [str], "tone": str}
- intent: what the buyer actually wants right now, in one short phrase.
- answerable_from_references: true only if the reference titles clearly cover it.
- needs_human: true if the buyer asks for a person/call/manager, wants to negotiate or confirm price/contract/terms, is upset, or it cannot be answered safely.
- points: 1-3 things the reply should cover, in order.
- avoid: exact sentences/openers already used in the recent assistant replies that must not be repeated.
- tone: e.g. "brief and warm"."""


async def plan_reply(*, buyer_text: str, recent: List[Dict[str, Any]], ref_titles: List[str]) -> Optional[Dict[str, Any]]:
    """Cheap pre-reply analysis. Returns None on any failure (the reply path then proceeds as before)."""
    if os.getenv("PLAN_BEFORE_REPLY", "1") == "0":
        return None
    convo = "\n".join(
        f"{'Buyer' if m.get('role') == 'user' else 'Assistant'}: {str(m.get('content'))[:300]}"
        for m in recent if m.get("role") in ("user", "assistant") and m.get("content")
    )[-1800:]
    user = f"Recent conversation:\n{convo or '(none)'}\n\nBuyer's new message:\n{buyer_text[:600]}\n\nReference titles found: {ref_titles[:6] or 'NONE'}"
    plan = await _llm_json(_PLANNER_SYSTEM, user, max_tokens=350, timeout=15.0)
    if not isinstance(plan, dict):
        return None
    plan["points"] = [str(p)[:160] for p in (plan.get("points") or [])][:3]
    plan["avoid"] = [str(p)[:160] for p in (plan.get("avoid") or [])][:6]
    return plan


def plan_note(plan: Dict[str, Any]) -> str:
    lines = ["Reply plan (private thinking, never mention it to the buyer):"]
    if plan.get("intent"):
        lines.append(f"- The buyer wants: {plan['intent']}")
    if plan.get("points"):
        lines.append("- Cover, in this order: " + "; ".join(plan["points"]))
    if plan.get("needs_human"):
        lines.append("- A human colleague is needed here: tell the buyer, naturally and briefly, that a colleague will pick this up on this chat. Do not guess.")
    if plan.get("avoid"):
        lines.append("- Do NOT reuse these earlier sentences: " + " | ".join(plan["avoid"]))
    if plan.get("tone"):
        lines.append(f"- Tone: {plan['tone']}")
    return "\n".join(lines)


# ----------------------------------------------------------------- distiller

_DISTILL_SYSTEM = """\
You improve a WhatsApp B2B sales assistant (Jane Tan, Petrobind Global: bitumen and base oil) from the sales team's feedback on its replies.
Turn the feedback into a few reusable lessons about HOW to reply (tone, length, structure, when to hand over to a human, what to ask).
Rules:
- Return ONLY JSON: {"lessons":[{"text": str, "kind": "style"|"behavior"|"knowledge"}]}
- At most 5 lessons. Imperative, general, under 200 characters each. No buyer names, numbers or quotes.
- NEVER state product facts, prices, specs, stock or lead times. If the feedback says a fact was wrong, use kind "knowledge" and write "Check the knowledge base for: <topic>".
- Skip anything already covered by the existing lessons. If feedback is too vague or contradictory, return fewer lessons or none."""


async def distill() -> Dict[str, Any]:
    """Turn unprocessed feedback into pending lessons. Returns a summary dict."""
    fb = [i for i in await fs.list_feedback() if not i.get("processed")]
    if not fb:
        return {"ok": True, "processed": 0, "new_lessons": 0, "note": "No new feedback to learn from."}
    existing = [l["text"] for l in await fs.list_lessons() if l.get("status") != "disabled"]
    lines = []
    for i in fb[:40]:
        bits = [f"[{i['rating'].upper()}]", f"Buyer: {i.get('buyer_text','')[:250]}", f"Reply: {i.get('ai_text','')[:350]}"]
        if i.get("tags"):
            bits.append("Tags: " + ", ".join(i["tags"]))
        if i.get("note"):
            bits.append(f"Team note: {i['note'][:300]}")
        if i.get("better_reply"):
            bits.append(f"Better reply: {i['better_reply'][:350]}")
        lines.append("\n".join(bits))
    user = "Existing lessons:\n" + ("\n".join(f"- {e}" for e in existing) or "(none)") + "\n\nFeedback:\n\n" + "\n\n".join(lines)
    out = await _llm_json(_DISTILL_SYSTEM, user, max_tokens=700, timeout=40.0)
    if not isinstance(out, dict):
        return {"ok": False, "processed": 0, "new_lessons": 0, "note": "The AI could not summarise the feedback right now. Try again."}
    new = await fs.add_lessons([l for l in (out.get("lessons") or []) if isinstance(l, dict)][:5], [i["id"] for i in fb])
    await fs.mark_processed([i["id"] for i in fb])
    return {"ok": True, "processed": len(fb), "new_lessons": len(new)}


# --------------------------------------------------------------------- stats

async def stats() -> Dict[str, Any]:
    fb = await fs.list_feedback()
    up = sum(1 for i in fb if i.get("rating") == "up")
    down = sum(1 for i in fb if i.get("rating") == "down")
    days: Dict[str, Dict[str, int]] = {}
    for i in fb:
        d = time.strftime("%Y-%m-%d", time.gmtime(i.get("ts", 0)))
        days.setdefault(d, {"up": 0, "down": 0})[i["rating"]] += 1
    daily = [{"date": d, **v} for d, v in sorted(days.items())][-14:]
    lessons = await fs.list_lessons()
    return {
        "total": len(fb), "up": up, "down": down,
        "approval_rate": round(100 * up / len(fb)) if fb else None,
        "unprocessed": sum(1 for i in fb if not i.get("processed")),
        "daily": daily,
        "lessons_active": sum(1 for l in lessons if l.get("status") == "active"),
        "lessons_pending": sum(1 for l in lessons if l.get("status") == "pending"),
    }
