"""Is a website page actually reachable? The AI sends product-page links to buyers and the Stock page shows them to the team,
so both check first (the live site once answered 404 for every product page because an old build was deployed)."""
import asyncio
import time
from typing import Any, Dict, Iterable, Tuple

import httpx

OK_TTL, BAD_TTL, UNKNOWN_TTL = 1800.0, 120.0, 30.0
_UA = "Mozilla/5.0 (compatible; PetrobindInbox/1.0; +link-check)"
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def clear() -> None:
    _cache.clear()


def _ttl(result: Dict[str, Any]) -> float:
    return OK_TTL if result["ok"] is True else BAD_TTL if result["ok"] is False else UNKNOWN_TTL


async def check(url: str) -> Dict[str, Any]:
    """{ok: True|False|None, status, checked_at}. None = could not tell (website unreachable from here)."""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _ttl(hit[1]):
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers={"User-Agent": _UA}) as c:
            r = await c.get(url)
        ctype = str(r.headers.get("content-type", "")).lower()
        result = {"ok": r.status_code == 200 and ("html" in ctype or not ctype), "status": r.status_code, "checked_at": time.time()}
    except Exception:
        result = {"ok": None, "status": None, "checked_at": time.time()}
    _cache[url] = (time.time(), result)
    return result


async def is_live(url: str) -> bool:
    """False only when the page is known to be broken (unknown counts as fine, so a network blip never hides every link)."""
    return (await check(url))["ok"] is not False


async def check_many(urls: Iterable[str], concurrency: int = 8) -> Dict[str, Dict[str, Any]]:
    unique = list(dict.fromkeys(urls))
    sem = asyncio.Semaphore(concurrency)

    async def one(u: str) -> Tuple[str, Dict[str, Any]]:
        async with sem:
            return u, await check(u)

    return dict(await asyncio.gather(*(one(u) for u in unique)))
