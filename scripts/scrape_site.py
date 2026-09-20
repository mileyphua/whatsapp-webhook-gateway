#!/usr/bin/env python3
"""One-time, dev-only headless-browser scraper for www.petrobindglobal.com.

Usage (from repo root):
    python3 scripts/scrape_site.py [--base https://www.petrobindglobal.com] [--only N]

Outputs one JSON file per page into rag/raw_pages/<slug>.json with shape:
    {"url": str, "title": str, "text": str, "word_count": int, "scraped_at": iso_ts}

Also writes a human-review manifest (rag/raw_pages/_REVIEW_MANIFEST.md) listing
the 10 most important pages (by topic) for spot-checking before go-live, per
PLAN.md Part 4.2.

Playwright is a DEV dependency only — it is not in Render's requirements.txt.
The raw_pages JSON artifacts it produces ARE committed to the repo so Render
(and anyone building the index) does not need a browser runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "rag" / "raw_pages"
RAW_DIR.mkdir(parents=True, exist_ok=True)

SITEMAP_SUFFIX = "/sitemap.xml"
# Common clientside-rendered content wrappers; if present, extract only within
# them to avoid header/footer/menu noise. Fallback to <body>.
MAIN_CONTENT_SELECTORS = [
    "main",
    "[role='main']",
    "#content",
    "#main",
    ".main-content",
    ".page-content",
    ".content",
    "article",
    "body",
]
SKIP_SELECTORS = ",".join(
    [
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        ".nav",
        ".navbar",
        ".menu",
        ".footer",
        ".header",
        "[aria-hidden='true']",
    ]
)

HUMAN_PRIORITY_PATTERNS = [
    ("bitumen-60/70", "Standard Bitumen 60/70 — flagship grade"),
    ("bitumen-80/100", "Standard Bitumen 80/100"),
    ("base-oil-sn150", "Base Oil SN150"),
    ("polymer-modified", "Polymer Modified Bitumen"),
    ("oxidized-bitumen", "Oxidized Bitumen grades overview"),
    ("bitumen-emulsion", "Bitumen Emulsion grades overview"),
    ("pricing", "Pricing explained / COA / grade-by-application"),
    ("storage", "Storage & handling guide"),
    ("about", "About Petrobind Global — company profile"),
    ("contact", "Contact page / email / WhatsApp for sales"),
]


def _slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "home"
    safe = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"
    return safe


def _normalize_url(base: str, url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme and parsed.netloc:
        return urlunparse(parsed._replace(fragment=""))
    # Relative
    base_p = urlparse(base)
    return urlunparse(
        base_p._replace(path=parsed.path, query=parsed.query, fragment="")
    )


async def parse_sitemap_via_http(base: str) -> list[str]:
    """Fetch sitemap via plain HTTP (sitemap.xml is static XML, no JS needed)."""
    import httpx

    sitemap_url = base.rstrip("/") + SITEMAP_SUFFIX
    print(f"[sitemap] fetching {sitemap_url}")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(sitemap_url)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:
        print(f"[sitemap] HTTP failed: {exc!r}; falling back to crawling homepage links")
        return []

    urls = re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", text, re.IGNORECASE)
    seen: set[str] = set()
    deduped: list[str] = []
    base_host = urlparse(base).netloc.lower()
    for u in urls:
        norm = _normalize_url(base, u)
        host = urlparse(norm).netloc.lower()
        if host != base_host:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        deduped.append(norm)
    deduped.sort()
    print(f"[sitemap] found {len(deduped)} unique same-host URLs")
    return deduped


async def parse_sitemap(page, base: str) -> list[str]:  # kept for fallbacks
    return await parse_sitemap_via_http(base)


async def scrape_one(page, url: str, *, timeout_ms: int = 60_000) -> dict | None:
    slug = _slug(url)
    out_path = RAW_DIR / f"{slug}.json"
    # Allow re-runs without re-scraping unchanged pages. Delete individual
    # JSON files to force a re-scrape.
    if out_path.exists():
        try:
            with out_path.open("r", encoding="utf-8") as f:
                prev = json.load(f)
            if prev.get("url") == url and prev.get("text"):
                print(f"  ✕ skip (already exists)  {url}")
                return prev
        except Exception:
            pass

    try:
        start = time.perf_counter()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if resp is None or resp.status >= 404:
            print(f"  ✗ HTTP {getattr(resp, 'status', None)}  {url}")
            return None
        # Small idle wait for CSR hydration — React/Vue apps often paint after
        # domcontentloaded. This is the main reason we use a browser.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            try:
                await page.wait_for_timeout(2500)
            except Exception:
                pass

        title = (await page.title()) or url
        # Drop non-content DOM then pull innerText from the deepest viable
        # main-content wrapper.
        await page.evaluate(f"""() => {{
            document.querySelectorAll({json.dumps(SKIP_SELECTORS)}).forEach(n => {{
                try {{ n.remove(); }} catch (e) {{}}
            }});
        }}""")
        text: str | None = None
        for sel in MAIN_CONTENT_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el is None:
                    continue
                t = (await el.inner_text()) or ""
                if len(t.split()) >= 40 or sel == "body":
                    text = t
                    break
            except Exception:
                continue
        if not text:
            text = (await page.inner_text("body")) or ""
        # Normalize whitespace.
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        wc = len(text.split())
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        data = {
            "url": url,
            "title": title.strip(),
            "text": text,
            "word_count": wc,
            "elapsed_ms": elapsed_ms,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  ✓ {wc:>5} words  {url}  → rag/raw_pages/{slug}.json")
        return data
    except Exception as exc:
        print(f"  ✗ error {exc!r}  {url}")
        return None


def build_review_manifest(all_pages: list[dict]) -> str:
    by_url = {p["url"]: p for p in all_pages if p}
    rows: list[str] = [
        "# Human Review Manifest — Petrobind raw scraped pages",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Total pages scraped: **{len(by_url)}**",
        "",
        "## Priority spot-check list (10 key pages — verify content accuracy before go-live)",
        "",
        "| # | Pattern | Expected page | Scraped? | Word count | File |",
        "|---|---------|---------------|----------|------------|------|",
    ]
    for idx, (pat, expected_name) in enumerate(HUMAN_PRIORITY_PATTERNS, 1):
        match = next(
            (p for p in by_url.values() if pat in (p["url"] + " " + p["title"]).lower()),
            None,
        )
        if match:
            rows.append(
                f"| {idx} | `{pat}` | {expected_name} | ✅ Yes | "
                f"{match['word_count']} | `rag/raw_pages/{_slug(match['url'])}.json` |"
            )
        else:
            rows.append(
                f"| {idx} | `{pat}` | {expected_name} | ❌ Not found in scraped pages | — | — |"
            )
    rows += [
        "",
        "## All pages scraped",
        "",
        "| File | URL | Title | Words |",
        "|------|-----|-------|-------|",
    ]
    for p in sorted(by_url.values(), key=lambda x: x["url"]):
        rows.append(
            f"| `{_slug(p['url'])}.json` | {p['url']} | "
            f"{p['title'].replace('|', '/')} | {p['word_count']} |"
        )
    rows.append("")
    return "\n".join(rows)


async def main(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        default="https://www.petrobindglobal.com",
        help="Site base URL (with protocol). Defaults to petrobindglobal.com.",
    )
    parser.add_argument(
        "--only",
        type=int,
        default=0,
        help="If >0, only scrape the first N URLs found (useful for a dry run).",
    )
    parser.add_argument(
        "--url-list",
        type=Path,
        default=None,
        help="Optional plain text file with one URL per line, used INSTEAD of the sitemap.",
    )
    args = parser.parse_args(list(argv))

    if args.url_list and args.url_list.exists():
        raw_urls = [ln.strip() for ln in args.url_list.read_text().splitlines() if ln.strip()]
    else:
        raw_urls = await parse_sitemap_via_http(args.base)

    # Import here so Playwright is not required to just import this module.
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 "
                "Safari/537.36 Petrobind-Scraper/1.0 (+https://www.petrobindglobal.com)"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        if not raw_urls:
            # Sitemap unavailable — start from homepage and pull same-host <a> links.
            hp = await scrape_one(page, args.base.rstrip("/") + "/")
            if hp is not None:
                hrefs = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.getAttribute('href'))",
                )
                seen: set[str] = set()
                for h in hrefs:
                    if not h or not isinstance(h, str):
                        continue
                    norm = _normalize_url(args.base, h)
                    if urlparse(norm).netloc.lower() != urlparse(args.base).netloc.lower():
                        continue
                    if norm in seen:
                        continue
                    seen.add(norm)
                    raw_urls.append(norm)
                raw_urls.sort()

        if args.only > 0:
            raw_urls = raw_urls[: args.only]
            print(f"[dry-run] --only={args.only}; truncating URL list")

        print(f"[scrape] {len(raw_urls)} URLs → rag/raw_pages/")
        results: list[dict] = []
        for i, url in enumerate(raw_urls, 1):
            print(f"[{i}/{len(raw_urls)}]", end=" ")
            res = await scrape_one(page, url)
            if res is not None:
                results.append(res)
            # Gentle rate-limit so we don't 429 a small site.
            await page.wait_for_timeout(350)

        await ctx.close()
        await browser.close()

    manifest = build_review_manifest(results)
    manifest_path = RAW_DIR / "_REVIEW_MANIFEST.md"
    manifest_path.write_text(manifest, encoding="utf-8")
    total_words = sum((r.get("word_count") or 0) for r in results)
    print(
        f"\nDone. Scraped {len(results)}/{len(raw_urls)} pages, "
        f"{total_words:,} total words.\n"
        f"→ Human review manifest: {manifest_path.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main(sys.argv[1:])))
