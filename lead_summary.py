"""Lead summary: what this buyer wants and where the chat stands, in plain words, for the person reading the inbox.

Built from the chat itself with fixed rules (no AI call): instant, free, and it can only report what the buyer or we
actually wrote. Placeholders such as "Unknown" or "N/A" never count as an answer.
"""
import re
from typing import Any, Dict, List, Optional

UNKNOWN = "unknown"
MAX_TOPICS = 12
SNIPPET = 160

_PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "null", "-", "tbd", "not provided", "not specified"}

# ---- reading the buyer's words -------------------------------------------------------------------------------------
_RE_OXIDIZED = re.compile(r"\bR\s?-?(\d{2}/\d{2})\b")
_RE_VG = re.compile(r"\bVG\s?-?(\d{2})\b", re.I)
_RE_EMULSION = re.compile(r"\b((?:CSS|SS|RS|MS|CRS|CMS)-?\d[hH]?)(?![\w/])", re.I)
_RE_PEN = re.compile(r"(?<![\w/])(\d{2,3})\s?/\s?(\d{2,3})(?![\w/])")
_RE_QTY = re.compile(r"(?<![\w/.])(\d[\d,]*(?:\.\d+)?)\s*(tonnes?|tons?|metric tons?|mts?)\b", re.I)
_RE_INCOTERM = re.compile(r"\b(FOB|CFR|CIF|EXW|DAP|DDP|CIP|FCA)\b", re.I)
_RE_PACKAGING = re.compile(r"\b(drums?|bulk|jumbo bags?|isotanks?|iso tanks?|flexitanks?|bags?)\b", re.I)
_RE_DEST = re.compile(r"(?:\bto|\bCFR|\bCIF|\bDAP|\bDDP|\bCIP)\s+([A-Z][\w-]*(?:\s[A-Z][\w-]*){0,2})")
_DEST_JUNK = {"Me", "You", "Us", "The", "Know", "Be", "Do", "Get", "Have", "See", "Confirm", "Proceed", "Ask", "Send"}

_RE_PRICE = re.compile(r"\b(price|prices|pricing|quote|quotation|how much|cost|rate)\b", re.I)
_RE_BUY = re.compile(r"\b(buy|purchase|order|proceed|place an order)\b", re.I)
_RE_WANT_QTY = re.compile(r"\b(need|want|looking for|require|interested in)\b", re.I)
_RE_PROCEED = re.compile(r"\b(proceed|place (?:the|an|my) order|confirm (?:the|my) order|go ahead)\b", re.I)
_RE_CALL = re.compile(r"\b(call|meeting|schedule|appointment|zoom|teams)\b", re.I)
_RE_CERT = re.compile(r"\b(certif\w*|iso|accredit\w*|licen[cs]e\w*)\b", re.I)
_RE_DOCS = re.compile(r"\b(coa|msds|sds|tds|pds|datasheet|data sheet|certificate of analysis|brochure)\b", re.I)
_RE_GREETING = re.compile(r"^\s*(hi|hello|hey|hola|salam|good (morning|afternoon|evening)|hi again|hello again)\b[\s!.,👋🙂]*$", re.I)
_RE_FORMAL = re.compile(r"\b(dear sir|kindly|regards|respected)\b", re.I)
_RE_SENT_DOC_TEXT = re.compile(r"\.pdf\b", re.I)

_TOPICS = [  # first match wins (order matters)
    ("Certifications", re.compile(r"\b(certif\w*|iso|accredit\w*|licen[cs]e\w*)\b", re.I)),
    ("Documents (COA / datasheet)", _RE_DOCS),
    ("Call or meeting", _RE_CALL),
    ("Comparing grades", re.compile(r"\b(difference|compare|comparison|versus|vs)\b", re.I)),
    ("Recommendation", re.compile(r"\b(recommend\w*|which grade|best for|suitable)\b", re.I)),
    ("Specifications", re.compile(r"\b(spec|specs|specification|specifications|penetration|viscosity|softening|flash point)\b", re.I)),
    ("Packaging", re.compile(r"\b(packag\w*|drums?|bags?|bulk)\b", re.I)),
    ("Shipping and delivery", re.compile(r"\b(ship\w*|deliver\w*|lead time|transit|port|incoterm)\b", re.I)),
    ("Product information", re.compile(r"\b(tell me more|what is|used for|uses|about|different from)\b", re.I)),
]

_NEXT_TEXT = {
    "human_take_over": "a person needs to take over this chat",
    "confirm_price_with_sales": "the sales director needs to confirm the price",
    "ask_requirement": "ask what they are sourcing, how much, and where to",
    "ask_product": "ask which product or grade they need",
    "ask_quantity": "ask how many tonnes they need",
    "ask_destination": "ask the destination port or country",
    "ask_contact": "ask for company name and contact details",
    "prepare_quote": "prepare the quote",
    "send_documents": "send the documents they asked for",
    "book_call": "offer a time for a call",
    "prepare_for_call": "prepare for the booked call",
}


def _clean(v: Any) -> str:
    s = str(v or "").strip()
    return "" if s.lower() in _PLACEHOLDERS else s


def _snippet(text: Any, n: int = SNIPPET) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _product_from(text: str) -> str:
    m = _RE_OXIDIZED.search(text)
    if m:
        return f"Oxidized bitumen R{m.group(1)}"
    if re.search(r"\boxidi[sz]ed\b", text, re.I):
        return "Oxidized bitumen"
    m = _RE_VG.search(text)
    if m:
        return f"VG{m.group(1)}"
    m = _RE_EMULSION.search(text)
    if m:
        g = re.match(r"([A-Za-z]+)-?(\d)([hH]?)", m.group(1))
        return f"Bitumen emulsion {g.group(1).upper()}-{g.group(2)}{g.group(3).lower()}"
    m = _RE_PEN.search(text)
    if m and 10 <= int(m.group(1)) <= 200 and int(m.group(1)) < int(m.group(2)):
        return f"Bitumen {m.group(1)}/{m.group(2)}"
    if re.search(r"\bemulsions?\b", text, re.I):
        return "Bitumen emulsion"
    return ""


def _dest_from(text: str) -> str:
    for m in _RE_DEST.finditer(text):
        name = m.group(1).strip()
        if name.split()[0] in _DEST_JUNK:
            continue
        return name
    return ""


def _topic_of(msg: Dict[str, Any]) -> str:
    text = str(msg.get("text") or "")
    if msg.get("media_type") and not text.strip():
        return "Attachment"
    if _RE_GREETING.match(text):
        return "Greeting"
    wants = bool(_RE_BUY.search(text) or (_RE_WANT_QTY.search(text) and _RE_QTY.search(text)))
    asks_price = bool(_RE_PRICE.search(text))
    if wants and asks_price:
        return "Order and price"
    if wants:
        return "Order"
    if asks_price:
        return "Price"
    for label, rx in _TOPICS:
        if rx.search(text):
            return label
    if re.search(r"\bmy company is\b|@\w+\.\w+", text, re.I):
        return "Contact details"
    return "Other"


def _is_outbound(m: Dict[str, Any]) -> bool:
    return m.get("direction") in ("ai", "human")


def _sent_document(m: Dict[str, Any]) -> bool:
    return _is_outbound(m) and (m.get("media_type") == "document" or bool(_RE_SENT_DOC_TEXT.search(str(m.get("text") or ""))))


def _build_topics(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for m in msgs:
        d = m.get("direction")
        if d == "buyer":
            label = (m.get("filename") and not m.get("text")) and f"[{m.get('media_type') or 'file'}] {m.get('filename')}" or ""
            cur = {"topic": _topic_of(m), "buyer": _snippet(m.get("text") or label or "[attachment]"), "reply": "", "by": None,
                   "at": m.get("created_at") or ""}
            out.append(cur)
        elif _is_outbound(m) and cur is not None and cur["by"] is None:
            cur["reply"] = _snippet(m.get("text") or (f"[{m.get('media_type')}] {m.get('filename') or ''}".strip() if m.get("media_type") else ""))
            cur["by"] = "human" if d == "human" else "ai"
    return out[-MAX_TOPICS:]


def build(messages: List[Dict[str, Any]], inquiry: Optional[Dict[str, Any]] = None,
          state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msgs = [m for m in (messages or []) if m.get("direction") in ("buyer", "ai", "human", "system")]
    inq = inquiry or {}
    st = state or {}
    buyer = [m for m in msgs if m.get("direction") == "buyer"]
    buyer_texts = [str(m.get("text") or "") for m in buyer]

    # ---- facts: saved lead details first, then what the text says (newest message wins) ----
    def newest(fn) -> str:
        for t in reversed(buyer_texts):
            v = fn(t)
            if v:
                return v
        return ""

    product = _clean(inq.get("product")) or newest(_product_from)
    qm = lambda t: (lambda m: f"{m.group(1)} {m.group(2).lower() if m.group(2).lower() != 'mt' else 'MT'}" if m else "")(_RE_QTY.search(t))
    quantity = _clean(inq.get("quantity")) or newest(qm)
    destination = _clean(inq.get("destination_port")) or newest(_dest_from)
    incoterm = _clean(inq.get("incoterm")) or newest(lambda t: (lambda m: m.group(1).upper() if m else "")(_RE_INCOTERM.search(t)))
    packaging = _clean(inq.get("packaging")) or newest(lambda t: (lambda m: m.group(1).lower() if m else "")(_RE_PACKAGING.search(t)))
    company = _clean(inq.get("company_name"))
    contact = " · ".join(x for x in (_clean(inq.get("contact_name")), _clean(inq.get("contact_position")), _clean(inq.get("contact_email"))) if x)

    # ---- what they asked for ----
    all_text = "\n".join(buyer_texts)
    price_requested = bool(_RE_PRICE.search(all_text))
    wants_to_buy = bool(_RE_BUY.search(all_text) or price_requested or (_RE_WANT_QTY.search(all_text) and _RE_QTY.search(all_text)))
    non_greeting = [t for t in buyer_texts if t.strip() and not _RE_GREETING.match(t)]
    if wants_to_buy:
        intent = "bitumen_purchase"
    elif _RE_CALL.search(all_text):
        intent = "call_request"
    elif _RE_CERT.search(all_text) or _RE_DOCS.search(all_text):
        intent = "documents_request"
    elif non_greeting:
        intent = "product_information"
    else:
        intent = "greeting_only"

    docs_sent = any(_sent_document(m) for m in msgs)
    last_price_idx = max((i for i, m in enumerate(msgs) if m.get("direction") == "buyer" and _RE_PRICE.search(str(m.get("text") or ""))), default=-1)
    price_answered = last_price_idx >= 0 and any(m.get("direction") == "human" for m in msgs[last_price_idx + 1:])
    price_open = price_requested and not price_answered

    # ---- stage ----
    contact_known = bool(company or contact)
    if st.get("booking_confirmed"):
        stage = "call_booked"
    elif _RE_PROCEED.search(all_text):
        stage = "ready_to_order"
    elif wants_to_buy and product and quantity and destination or (wants_to_buy and product and contact_known):
        stage = "qualified_lead"
    elif len(buyer) >= 3 or product or intent in ("product_information", "documents_request", "call_request"):
        stage = "exploring"
    else:
        stage = "new_lead"

    # ---- what to do next ----
    paused = bool(st.get("ai_paused"))
    handed = bool(st.get("needs_human_since"))
    if paused or handed:
        nxt = "human_take_over"
    elif price_open:
        nxt = "confirm_price_with_sales"
    elif intent == "call_request":
        nxt = "prepare_for_call" if st.get("booking_confirmed") else "book_call"
    elif intent == "documents_request" and not docs_sent:
        nxt = "send_documents"
    elif intent == "bitumen_purchase":
        nxt = ("ask_product" if not product else "ask_quantity" if not quantity else "ask_destination" if not destination
               else "ask_contact" if not contact_known else "prepare_quote")
    else:
        nxt = "ask_requirement"

    # ---- tone ----
    counts = [len(t.split()) for t in buyer_texts if t.strip()]
    avg = (sum(counts) / len(counts)) if counts else 0
    tone = "formal" if _RE_FORMAL.search(all_text) else "short_business" if avg <= 8 else "detailed" if avg > 30 else "conversational"

    # ---- what a person must confirm ----
    confirm: List[str] = []

    def add(x: str) -> None:
        if x and x not in confirm:
            confirm.append(x)

    if paused:
        add(f"AI is paused ({_clean(st.get('ai_paused_reason')) or 'could not read the buyer’s message'}): a person must read it and reply")
    if handed:
        add(f"Handed to a person: {_clean(st.get('needs_human_reason')) or 'the AI asked for help'}")
    if price_open:
        what = " ".join(x for x in (quantity, product, f"to {destination}" if destination else "", incoterm) if x)
        add(f"Price{(' for ' + what) if what else ''}: only the sales director can confirm it")
    if _RE_CERT.search(all_text):
        add("Buyer asked about certifications: a person should confirm which apply")
    if _RE_DOCS.search(all_text) and not docs_sent:
        add("Buyer asked for documents (COA / datasheet): a person should send them")
    if stage in ("ready_to_order", "qualified_lead") and quantity and destination:
        add(f"Order details to confirm: {quantity} {product or 'product'} to {destination}"
            f"{(', ' + incoterm) if incoterm else ''}{(', ' + packaging) if packaging else ''}: check availability, lead time and terms")
    last_human_idx = max((i for i, m in enumerate(msgs) if m.get("direction") == "human"), default=-1)
    if not confirm:
        for m in msgs[last_human_idx + 1:]:
            if m.get("direction") == "ai" and re.search(r"\b(colleague|sales director|our team) (will|to)\b|let me check with|have a colleague", str(m.get("text") or ""), re.I):
                add("The AI told the buyer a colleague would follow up")
                break
    failed = [m for m in msgs if _is_outbound(m) and m.get("errored")]
    if failed:
        add(f"A message failed to send ({_snippet(failed[-1].get('error_detail') or 'unknown error', 80)}): resend it")
    if msgs and msgs[-1].get("direction") == "buyer" and not paused:
        add("The buyer’s latest message has no reply yet")

    # ---- the story ----
    topics = _build_topics(msgs)
    need_bits = [x for x in (product, quantity, incoterm, f"to {destination}" if destination else "", packaging) if x]
    last_buyer = buyer[-1] if buyer else None
    last_out = next((m for m in reversed(msgs) if _is_outbound(m)), None)
    parts = []
    if intent == "greeting_only":
        parts.append("The buyer has only said hello so far.")
    else:
        verb = {"bitumen_purchase": "wants to buy", "call_request": "asked for a call about", "documents_request": "asked for documents about",
                "product_information": "is asking about"}[intent]
        parts.append(f"The buyer {verb} {', '.join(need_bits) if need_bits else 'a product (not named yet)'}.")
    if last_buyer:
        parts.append(f"Last message from the buyer: “{_snippet(last_buyer.get('text') or '[attachment]', 120)}”.")
    if last_out:
        who = "a person" if last_out.get("direction") == "human" else "the AI"
        parts.append(f"Our last reply ({who}): “{_snippet(last_out.get('text') or '[file]', 120)}”.")
    parts.append(f"Where it stands: {_NEXT_TEXT[nxt]}.")

    return {
        "relationship_stage": stage,
        "customer_intent": intent,
        "product_interest": product or UNKNOWN,
        "quantity": quantity or UNKNOWN,
        "destination": destination or UNKNOWN,
        "incoterm": incoterm or UNKNOWN,
        "packaging": packaging or UNKNOWN,
        "company": company or UNKNOWN,
        "contact": contact or UNKNOWN,
        "price_requested": price_requested,
        "technical_docs_sent": docs_sent,
        "next_best_action": nxt,
        "next_best_action_text": _NEXT_TEXT[nxt],
        "tone": tone,
        "remark": " ".join(parts),
        "needs_human_confirmation": confirm,
        "topics": topics,
        "buyer_messages": len(buyer),
        "last_buyer_at": (last_buyer or {}).get("created_at") or "",
    }
