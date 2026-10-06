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
        return re.compile(r"(?<![\w/.])(?:" + "|".join(re.escape(a) for a in sorted(self.aliases, key=len, reverse=True)) + r")(?![\w/])", re.I)


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
