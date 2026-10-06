"""What can the AI talk about? Every product page of the knowledge base is an item with an on/off switch (the Stock
page). Off = not in stock: the AI must not mention, offer or recommend it.

This module knows the items, the names buyers use for them (60/70, 60-70, CSS-1h, css1h ...), and which are switched off.
Storage is the same small Redis hash / local file used by feedback_store, so it survives restarts and is shared with Render."""
import importlib
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import feedback_store as fs
from reply_guard import split_sentences

_rag = importlib.import_module("rag.retrieve")

STOCK = "catalog_stock"
CACHE_SECONDS = 15.0

FAMILIES: List[Tuple[str, str]] = [
    ("bitumen", "Bitumen (penetration & viscosity grades)"),
    ("emulsion", "Bitumen emulsions"),
    ("oxidized", "Oxidized bitumen"),
    ("pmb", "Polymer-modified bitumen"),
    ("base-oil", "Base oil"),
]
FAMILY_NAMES = dict(FAMILIES)
# the three overview pages describe a whole family (and list its grades): they stay on until every grade of the family is off
OVERVIEW_PAGES = {"products-bitumen": "bitumen", "products-bitumen-emulsion": "emulsion", "products-oxidized-bitumen": "oxidized"}
_BASE_OIL = "products-base-oil-sn150"      # the site has no SN150 page: the scraped "page" is the Contact page, which must stay


_PATTERNS: Dict[str, "re.Pattern[str]"] = {}


@dataclass(frozen=True)
class Item:
    slug: str
    title: str
    family: str
    url: str
    aliases: Tuple[str, ...]

    @property
    def has_page(self) -> bool:
        return "/products/" in self.url

    def pattern(self) -> "re.Pattern[str]":
        pat = _PATTERNS.get(self.slug)
        if pat is None:
            pat = _PATTERNS[self.slug] = re.compile(
                r"(?<![\w/.])(?:" + "|".join(re.escape(a) for a in sorted(self.aliases, key=len, reverse=True)) + r")(?![\w/])", re.I)
        return pat


def _family(slug: str) -> str:
    if slug == "products-polymer-modified-bitumen":
        return "pmb"
    if slug == _BASE_OIL:
        return "base-oil"
    if slug.startswith("products-bitumen-emulsion-"):
        return "emulsion"
    if slug.startswith("products-oxidized-"):
        return "oxidized"
    return "bitumen"


def _variants(token: str) -> Set[str]:
    t = token.lower().strip()
    out = {t, t.replace(" ", ""), t.replace(" ", "-"), t.replace("-", " "), t.replace("-", "")}
    if "/" in t:
        out |= {t.replace("/", "-"), t.replace("/", " / ")}
    return {x for x in out if x}


def _make(slug: str, page_title: str, url: str) -> Item:
    fam = _family(slug)
    if slug == _BASE_OIL:
        return Item(slug, "Base Oil SN150", fam, "", tuple(sorted({"base oil sn150", "base oil", "sn150", "sn 150", "sn-150"})))
    title = page_title.split(" Supplier")[0].strip()
    if fam == "pmb":
        return Item(slug, title, fam, url, tuple(sorted({"polymer modified bitumen", "polymer-modified bitumen", "polymer modified", "pmb"})))
    token = re.sub(r"^(?:Bitumen Emulsion|Oxidized Bitumen|Bitumen)\s+", "", title).strip()
    return Item(slug, title, fam, url, tuple(sorted(_variants(token))))


_items_cache: Optional[List[Item]] = None


def items() -> List[Item]:
    """Every switchable product page, in the knowledge base's own order."""
    global _items_cache
    if _items_cache is None:
        out: List[Item] = []
        for p in _rag.all_pages():
            slug = p["slug"]
            if slug.startswith("products-") and slug not in OVERVIEW_PAGES:
                out.append(_make(slug, p["title"], p["url"]))
        _items_cache = out
    return list(_items_cache)


def by_slug() -> Dict[str, Item]:
    return {i.slug: i for i in items()}


def mentioned(text: str, candidates: Optional[List[Item]] = None) -> List[Item]:
    """The items a piece of text names (in any of their spellings)."""
    t = text or ""
    return [i for i in (candidates if candidates is not None else items()) if i.pattern().search(t)]


# ---------------------------------------------------------------- storage (switch state)
_cache: Dict[str, Any] = {"ts": 0.0, "status": {}}


def invalidate() -> None:
    _cache["ts"] = 0.0
    _cache["status"] = {}


async def _status() -> Dict[str, Dict[str, Any]]:
    if time.time() - _cache["ts"] < CACHE_SECONDS:
        return _cache["status"]
    data = await fs._hgetall(STOCK)
    _cache["status"], _cache["ts"] = data, time.time()
    return data


async def is_on(slug: str) -> bool:
    return bool((await _status()).get(slug, {}).get("on", True))


async def disabled_items() -> List[Item]:
    st = await _status()
    return [i for i in items() if not st.get(i.slug, {}).get("on", True)]


async def hidden_slugs() -> Set[str]:
    """Knowledge-base pages the AI must not retrieve: off items that own a page, and an overview page once its whole family is off."""
    off = await disabled_items()
    hidden = {i.slug for i in off if i.has_page}
    off_slugs = {i.slug for i in off}
    for page, fam in OVERVIEW_PAGES.items():
        members = [i.slug for i in items() if i.family == fam]
        if members and all(s in off_slugs for s in members):
            hidden.add(page)
    return hidden


async def set_item(slug: str, on: bool, by: str) -> Dict[str, Any]:
    if slug not in by_slug():
        raise ValueError("unknown item")
    entry = {"on": bool(on), "by": by, "ts": time.time()}
    await fs._hset(STOCK, slug, entry)
    invalidate()
    return entry


async def set_family(family: str, on: bool, by: str) -> int:
    if family not in FAMILY_NAMES:
        raise ValueError("unknown family")
    n = 0
    for i in items():
        if i.family == family:
            await fs._hset(STOCK, i.slug, {"on": bool(on), "by": by, "ts": time.time()})
            n += 1
    invalidate()
    return n


async def snapshot() -> Dict[str, Any]:
    st = await _status()
    fams, off_total = [], 0
    for fid, name in FAMILIES:
        rows = []
        for i in items():
            if i.family != fid:
                continue
            e = st.get(i.slug, {})
            rows.append({"slug": i.slug, "title": i.title, "url": i.url if i.has_page else "", "on": bool(e.get("on", True)),
                         "by": e.get("by", ""), "ts": e.get("ts")})
        on_count = sum(1 for r in rows if r["on"])
        off_total += len(rows) - on_count
        fams.append({"id": fid, "name": name, "total": len(rows), "on_count": on_count, "items": rows})
    return {"families": fams, "off_total": off_total}


# ---------------------------------------------------------------- keeping off items out of what the AI says
FAMILY_WORDS = {
    "emulsion": re.compile(r"\bemulsions?\b", re.I),
    "oxidized": re.compile(r"\boxidi[sz]ed\b|\bblown bitumen\b", re.I),
    "pmb": re.compile(r"\bpolymer[- ]modified\b|\bpmb\b", re.I),
    "base-oil": re.compile(r"\bbase oils?\b|\bsn ?-?150\b", re.I),
}
_UNAVAILABLE = re.compile(r"(not available|unavailable|isn'?t available|aren'?t available|out of stock|not in stock|can'?t supply|don'?t have|do not have|not currently)", re.I)
_GRADE_TOKEN = re.compile(r"(?<![\w/])(?:[A-Za-z]{1,4}-?\d{1,3}[a-z]?(?:[/-]\d{1,3}[a-z]?)?|\d{2,3}[/-]\d{1,3})(?![\w/])")


def fully_off_families(off: List[Item]) -> List[str]:
    off_slugs = {i.slug for i in off}
    out = []
    for fid, _ in FAMILIES:
        members = [i.slug for i in items() if i.family == fid]
        if members and all(m in off_slugs for m in members):
            out.append(fid)
    return out


def asked_for(text: str, off: List[Item]) -> Tuple[List[Item], List[str]]:
    """What the buyer named that is out of stock: (items by name, whole families by word, e.g. 'oxidized')."""
    named = mentioned(text, off)
    fams = [f for f in fully_off_families(off) if f in FAMILY_WORDS and FAMILY_WORDS[f].search(text or "")]
    return named, fams


def names_something_in_stock(text: str, off: List[Item]) -> bool:
    off_slugs = {i.slug for i in off}
    return any(i.slug not in off_slugs for i in mentioned(text))


def alternatives(item_or_family: Any, off: List[Item], limit: int = 2) -> List[Item]:
    """In-stock items closest to an unavailable one (neighbours in the same family; otherwise none)."""
    off_slugs = {i.slug for i in off}
    fam = item_or_family.family if isinstance(item_or_family, Item) else item_or_family
    pool = [i for i in items() if i.family == fam]
    if isinstance(item_or_family, Item):
        pos = next((n for n, i in enumerate(pool) if i.slug == item_or_family.slug), 0)
        ranked = sorted([(abs(n - pos), n, i) for n, i in enumerate(pool) if i.slug not in off_slugs and i.has_page], key=lambda t: (t[0], t[1]))
        return [i for _, _, i in ranked[:limit]]
    return []


def unavailable_reply(named: List[Item], fams: List[str], off: List[Item]) -> Tuple[str, Optional[Item]]:
    """What the AI says (without calling the model) when the buyer asks for something that is out of stock."""
    names = [i.title for i in named[:2]] + [FAMILY_NAMES[f].lower() for f in fams if not named]
    if len(named) > 2:
        names = [i.title for i in named[:2]] + ["the other grades you named"]
    if not names:
        names = ["that"]
    verb = "isn't" if len(names) == 1 else "aren't"
    first = f"Sorry, {' and '.join(names)} {verb} available at the moment."
    alts: List[Item] = []
    for it in named[:2]:
        for a in alternatives(it, off):
            if a not in alts:
                alts.append(a)
    alts = alts[:2]
    if alts:
        near = " and ".join(a.title for a in alts)
        return f"{first} {near} {'is' if len(alts) == 1 else 'are'} the closest option{'' if len(alts) == 1 else 's'} we can offer.\n\nWould you like to know more about {alts[0].title}?", alts[0]
    return f"{first} Is there another product I can help you with?", None


def unavailable_note(off: List[Item]) -> str:
    """System-prompt block: what is out of stock right now."""
    if not off:
        return ""
    whole = fully_off_families(off)
    parts = [f"ALL of: {FAMILY_NAMES[f]}" for f in whole]
    for fid, name in FAMILIES:
        if fid in whole:
            continue
        titles = [i.title for i in off if i.family == fid]
        if titles:
            parts.append(f"{name}: " + ", ".join(titles))
    return ("OUT OF STOCK right now (switched off by the sales team): " + "; ".join(parts) + ". "
            "Never mention, list, compare, offer or recommend these in any reply, and leave them out of every list of grades or products, even if the "
            "reference material, the company profile above or your own knowledge mentions them. Only if the buyer names one of them, say plainly that it is "
            "not available at the moment and suggest the closest in-stock alternative.")


def _tokens_removed(sentence: str, pats: List["re.Pattern[str]"]) -> str:
    t = sentence
    for p in pats:
        t = p.sub("", t)
    t = re.sub(r"\s*,\s*(,\s*)+", ", ", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\b(and|or)\s*,", r"\1", t)
    t = re.sub(r"\s*,\s*(and|or)\s+(?=[.!?]|$)", "", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"\s+([,.;!?])", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"^\s*[,;]\s*", "", t).strip()


def scrub_text(text: str, off: List[Item], families_off: Optional[List[str]] = None) -> str:
    """Remove every trace of out-of-stock items from a piece of text: whole sentences about them, their name inside a list of
    grades, and the 'related grades' cards on the site pages (headline lines plus that card's description)."""
    if not text or not off:
        return text
    pats = [i.pattern() for i in off]
    fam_pats = [FAMILY_WORDS[f] for f in (families_off if families_off is not None else fully_off_families(off)) if f in FAMILY_WORDS]
    anyp = pats + fam_pats

    def hits(s: str) -> bool:
        return any(p.search(s) for p in anyp)

    if not hits(text):
        return text
    paras = re.split(r"\n\s*\n", text)
    out: List[str] = []
    skip_description = False
    for para in paras:
        if skip_description:
            skip_description = False
            if not hits(para) and len(para.split()) >= 8 and "\n" not in para.strip():
                continue                                    # the description of a card whose headline was removed
        kept: List[str] = []
        for ln in para.split("\n"):
            if not hits(ln):
                kept.append(ln); continue
            stripped = ln.strip()
            label_like = not re.search(r"[.!?]$", stripped) or stripped.upper() == stripped
            if len(stripped) <= 70 and label_like and not _UNAVAILABLE.search(ln):
                if stripped.upper() == stripped or stripped.upper().startswith("GRADE "):
                    skip_description = True                  # a card headline like "GRADE 35/50" / "BITUMEN 35/50"
                continue                                    # a short label / link line such as "View 35/50 specs"
            sentences = split_sentences(ln) or [ln]
            fixed: List[str] = []
            for sent in sentences:
                if not hits(sent):
                    fixed.append(sent); continue
                if _UNAVAILABLE.search(sent):
                    fixed.append(sent); continue            # "Sorry, 60/70 isn't available" may name it
                if len(_GRADE_TOKEN.findall(sent)) >= 3:    # an enumeration of grades: drop only the off one(s)
                    cleaned = _tokens_removed(sent, pats)
                    if cleaned and not hits(cleaned):
                        fixed.append(cleaned)
                    continue
                # prose about the off item alone: drop the sentence
            if fixed:
                kept.append(" ".join(fixed))
        joined = "\n".join(kept).strip()
        if joined:
            out.append(joined)
    return "\n\n".join(out)
