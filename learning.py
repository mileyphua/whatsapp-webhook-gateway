"""Feedback-driven *skills* for the WhatsApp assistant.

The model itself is not fine-tuned. Instead the assistant has a library of skills,
modelled on Claude skills (SKILL.md):
  - `description` says WHEN a skill applies. The catalog of descriptions is always
    visible to the planner (cheap, like a skill's frontmatter).
  - `instructions` say WHAT to do and why. They are loaded into the reply prompt
    only for the skills that apply to this message (or always, for `always` skills).

Skills come from two places: built-in ones below, and ones learned from human
feedback (feedback_store). distill() turns new 👍/👎 feedback into skill proposals
(new skills, or revisions of existing ones); nothing changes the replies until a
human approves it. Skills never carry product facts: facts come from the RAG
knowledge base only.
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import feedback_store as fs

# ---------------------------------------------------------------- built-ins

BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "builtin-human-voice", "builtin": True, "always": True, "status": "active",
        "name": "Sound like a person",
        "description": "Applies to every reply. How to phrase a WhatsApp message so it reads like a real colleague, not a template.",
        "instructions": (
            "Reply to what this buyer just said, in your own words. A real person doesn't recycle sentences, so never reuse a "
            "sentence, opener or closing line you already sent earlier in this chat, and don't restate facts or links you already gave "
            "unless the buyer asks again. Skip stock phrases (\"Great question\", \"I hope this helps\", \"Feel free to reach out\") and "
            "repeated thank-yous. Keep it short for WhatsApp, usually 1-3 sentences: answer first, then at most one useful follow-up question."
        ),
    },
    {
        "id": "builtin-hand-over", "builtin": True, "always": False, "status": "active",
        "name": "Hand over to a human",
        "description": "Use when the buyer asks for a person, a call or a manager, wants to negotiate or confirm price, contract or payment terms, is upset or frustrated, or the question cannot be answered from the reference material.",
        "instructions": (
            "Say, in one short natural sentence, that a colleague will pick this up on this chat. Don't promise a time and don't guess "
            "facts, because a wrong answer costs more trust than a short wait. For price questions follow the company profile's pricing wording. "
            "If you already told them a colleague is coming, don't repeat that sentence: acknowledge the wait briefly in new words, or "
            "answer anything you can from the references."
        ),
    },
]

MAX_ALWAYS = 3
MAX_SELECTED = 3
MAX_EXAMPLES = 3
AUTO_LEARN_AFTER = 5  # unprocessed ratings before a distill run is triggered
_CACHE_TTL = 60.0
_cache: Dict[str, Any] = {"at": 0.0, "skills": [], "examples": []}


def invalidate_cache() -> None:
    _cache["at"] = 0.0


async def active_skills() -> List[Dict[str, Any]]:
    """Built-in + approved learned skills (cached 60 s). Never raises."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL:
        return _cache["skills"]
    skills = [dict(b) for b in BUILTIN_SKILLS]
    examples: List[Tuple[str, str]] = []
    try:
        skills += [s for s in await fs.list_skills("active")]
        for i in await fs.list_feedback():
            if len(examples) >= MAX_EXAMPLES:
                break
            if i.get("rating") == "down" and i.get("better_reply") and i.get("buyer_text"):
                examples.append((i["buyer_text"], i["better_reply"]))
            elif i.get("rating") == "up" and i.get("buyer_text"):
                examples.append((i["buyer_text"], i["ai_text"]))
    except Exception as exc:
        print(f"[learning] active_skills failed: {type(exc).__name__}: {exc!s}")
    _cache.update(at=now, skills=skills, examples=examples)
    return skills


def catalog_text(skills: List[Dict[str, Any]]) -> str:
    """The always-visible part: id, name and WHEN to use, for skills that are not always-on."""
    return "\n".join(f"- {s['id']}: {s['name']}. Use when: {s['description']}" for s in skills if not s.get("always"))


_TOKEN = re.compile(r"[a-z0-9]{4,}")


def select_without_planner(skills: List[Dict[str, Any]], buyer_text: str) -> List[str]:
    """Fallback when the planner is unavailable: pick skills whose description shares words with the message."""
    words = set(_TOKEN.findall(buyer_text.lower()))
    scored = []
    for s in skills:
        if s.get("always"):
            continue
        overlap = len(words & set(_TOKEN.findall((s["name"] + " " + s["description"]).lower())))
        if overlap:
            scored.append((overlap, s["id"]))
    scored.sort(reverse=True)
    return [i for _, i in scored[:MAX_SELECTED]]


def build_guidance(skills: List[Dict[str, Any]], selected_ids: List[str]) -> str:
    """System-prompt block: always-on skills + the skills that apply to this message + approved examples."""
    by_id = {s["id"]: s for s in skills}
    always = [s for s in skills if s.get("always")][:MAX_ALWAYS]
    chosen = [by_id[i] for i in selected_ids if i in by_id and not by_id[i].get("always")][:MAX_SELECTED]
    parts = []
    for s in always + chosen:
        parts.append(f"Skill: {s['name']}\n{s['instructions']}")
    if not parts:
        return ""
    block = "Follow these skills when writing the reply:\n\n" + "\n\n".join(parts)
    ex = _cache.get("examples") or []
    if ex:
        block += "\n\nReplies the team approved (match their tone and brevity; do not copy the content):\n" + "\n".join(
            f"Buyer: {b[:300]}\nGood reply: {g[:500]}" for b, g in ex[:MAX_EXAMPLES])
    return block


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
You plan a WhatsApp reply for Petrobind's sales assistant BEFORE it is written. Think about the buyer; do not write the reply.
Return ONLY JSON: {"intent": str, "needs_human": bool, "human_reason": str, "skills": [str], "points": [str], "avoid": [str], "tone": str}
- intent: what the buyer actually wants right now, in one short phrase.
- needs_human: true if the buyer asks for a person/call/manager, wants to negotiate or confirm price/contract/terms, is upset, or it cannot be answered safely from the reference titles.
- skills: ids from the skill catalog whose "Use when" matches this message (0-3). Only ids from the catalog.
- points: 1-3 things the reply should cover, in order.
- avoid: exact sentences/openers already used in the recent assistant replies that must not be repeated.
- tone: e.g. "brief and warm"."""


async def plan_reply(*, buyer_text: str, recent: List[Dict[str, Any]], ref_titles: List[str],
                     skills: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Cheap pre-reply analysis. Returns None on any failure (the reply path then proceeds as before)."""
    if os.getenv("PLAN_BEFORE_REPLY", "1") == "0":
        return None
    convo = "\n".join(
        f"{'Buyer' if m.get('role') == 'user' else 'Assistant'}: {str(m.get('content'))[:300]}"
        for m in recent if m.get("role") in ("user", "assistant") and m.get("content")
    )[-1800:]
    cat = catalog_text(skills or [])
    user = (f"Skill catalog:\n{cat or '(none)'}\n\nRecent conversation:\n{convo or '(none)'}\n\n"
            f"Buyer's new message:\n{buyer_text[:600]}\n\nReference titles found: {ref_titles[:6] or 'NONE'}")
    plan = await _llm_json(_PLANNER_SYSTEM, user, max_tokens=400, timeout=15.0)
    if not isinstance(plan, dict):
        return None
    valid = {s["id"] for s in (skills or [])}
    plan["skills"] = [str(i) for i in (plan.get("skills") or []) if str(i) in valid][:MAX_SELECTED]
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
You maintain the skill library of a WhatsApp B2B sales assistant (Jane Tan, Petrobind Global: bitumen and base oil). Skills are written like Claude SKILL.md files and \
are built from the sales team's feedback on the assistant's replies.

A skill has:
- name: short, 2-5 words (e.g. "Pricing questions", "First greeting").
- description: WHEN to use it, so the assistant can recognise the situation. Be specific and a little generous about triggers. Under 280 characters.
- instructions: WHAT to do and WHY, imperative, general and reusable, under 850 characters. Explain the reason behind each rule rather than shouting commands.
- always: true only for habits that apply to every reply (tone, brevity); otherwise false.

Return ONLY JSON: {"skills":[{"name":str,"description":str,"instructions":str,"always":bool}], "knowledge_checks":[str]}
Rules:
- Group related feedback into ONE skill per situation. If feedback fits an EXISTING skill, return that skill with the SAME name and a revised full description + instructions that fold the new feedback in. Do not make near-duplicates.
- At most 4 skills. If feedback is vague, contradictory or already covered by an existing or built-in skill, return fewer (or none).
- NEVER include product facts, prices, specs, stock or lead times in a skill. If the feedback says a FACT was wrong, put "Check the knowledge base for: <topic>" in knowledge_checks instead.
- No buyer names, phone numbers or quotes."""


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_distill_running = False


async def distill() -> Dict[str, Any]:
    """Turn unprocessed feedback into skill proposals. Single-flight: an overlapping call returns busy
    (several quick ratings must not create duplicate skills)."""
    global _distill_running
    if _distill_running:
        return {"ok": True, "busy": True, "processed": 0, "new_skills": 0, "updated_skills": 0, "note": "Already learning from your feedback. Check back in a moment."}
    _distill_running = True
    try:
        return await _distill_once()
    finally:
        _distill_running = False


async def _distill_once() -> Dict[str, Any]:
    fb = [i for i in await fs.list_feedback() if not i.get("processed")]
    if not fb:
        return {"ok": True, "processed": 0, "new_skills": 0, "updated_skills": 0, "note": "No new feedback to learn from."}
    stored = [s for s in await fs.list_skills() if s.get("status") != "disabled"]
    builtin_names = [b["name"] for b in BUILTIN_SKILLS]
    existing = "\n".join(f"- {s['name']} | when: {s['description']} | do: {s['instructions']}" for s in stored) or "(none)"
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
    user = (f"Built-in skills (already exist, don't duplicate): {', '.join(builtin_names)}\n\nExisting learned skills:\n{existing}\n\n"
            "Feedback:\n\n" + "\n\n".join(lines))
    out = await _llm_json(_DISTILL_SYSTEM, user, max_tokens=1400, timeout=45.0)
    if not isinstance(out, dict):
        return {"ok": False, "processed": 0, "new_skills": 0, "updated_skills": 0, "note": "The AI could not summarise the feedback right now. Try again."}
    src = [i["id"] for i in fb]
    by_name = {_norm_name(s["name"]): s for s in stored}
    taken = {_norm_name(n) for n in builtin_names}
    new = upd = 0
    for sk in [x for x in (out.get("skills") or []) if isinstance(x, dict)][:4]:
        name, desc, instr = str(sk.get("name") or ""), str(sk.get("description") or ""), str(sk.get("instructions") or "")
        key = _norm_name(name)
        if not (key and desc.strip() and instr.strip()) or key in taken:
            continue
        try:
            if key in by_name:
                if await fs.propose_skill_update(by_name[key]["id"], description=desc, instructions=instr, always=bool(sk.get("always")), source_ids=src):
                    upd += 1
            else:
                await fs.create_skill(name=name, description=desc, instructions=instr, always=bool(sk.get("always")), status="pending", source_ids=src)
                new += 1
        except ValueError:
            continue
    await fs.add_lessons([{"text": str(t), "kind": "knowledge"} for t in (out.get("knowledge_checks") or [])][:5], src)
    await fs.mark_processed(src)
    return {"ok": True, "processed": len(fb), "new_skills": new, "updated_skills": upd}


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
    skills = await fs.list_skills()
    return {
        "total": len(fb), "up": up, "down": down,
        "approval_rate": round(100 * up / len(fb)) if fb else None,
        "unprocessed": sum(1 for i in fb if not i.get("processed")),
        "daily": daily,
        "skills_active": len(BUILTIN_SKILLS) + sum(1 for s in skills if s.get("status") == "active"),
        "skills_waiting": sum(1 for s in skills if s.get("status") == "pending" or s.get("proposal")),
    }
