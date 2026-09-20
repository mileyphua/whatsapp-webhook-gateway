#!/usr/bin/env python3
"""One-time, offline build of rag/index.json from rag/raw_pages/*.json.

Usage (from repo root):
    python3 scripts/build_index.py [--chunk-size 500] [--overlap 120]

Requirements:
    - OPENAI_API_KEY available in env (loaded from .env if present).
    - One or more JSON files already produced by scripts/scrape_site.py
      under rag/raw_pages/<slug>.json.

Outputs rag/index.json:
    {
      "built_at": iso_ts,
      "model": "text-embedding-3-small",
      "chunk_size": 500,
      "overlap": 120,
      "page_count": N,
      "chunk_count": M,
      "chunks": [
        {"url": str, "title": str, "page_slug": str,
         "chunk_index": int, "chunk_text": str, "embedding": [float, ...]},
        ...
      ]
    }

Run this script whenever rag/raw_pages/ changes (site content refreshed / fixes)
and commit the resulting rag/index.json to the repo (used at request time on
Render — no live scraping or live embeddings build in production).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "rag" / "raw_pages"
INDEX_PATH = ROOT / "rag" / "index.json"
BATCH_SIZE = 64  # OpenAI embeddings API accepts up to 2048 per call; 64 is safe
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv() -> bool:  # type: ignore[misc]
        return False

from rag.retrieve import EMBEDDING_DIM, EMBEDDING_MODEL


def iter_raw_pages() -> Iterable[dict]:
    if not RAW_DIR.exists():
        return
    for path in sorted(RAW_DIR.glob("*.json")):
        if path.name.startswith("_"):  # skip _REVIEW_MANIFEST.md, _meta.json etc
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[build_index] skip {path.name}: {exc!r}")
            continue
        if not data.get("url") or not (data.get("text") or "").strip():
            continue
        data["_slug"] = path.stem
        yield data


def _split_tokens_approx(text: str, chunk_chars: int, overlap: int) -> list[str]:
    """Split text into character-window chunks that roughly map to tokens.

    Why char windows + sentence-aware instead of a tokenizer?
      - Works offline without installing tiktoken.
      - ~4 chars ≈ 1 token for English; we use chunk_chars ~4× chunk_tokens.
      - Slight over-chunking is cheap; under-chunking breaks retrieval.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(cursor + chunk_chars, len(text))
        if end < len(text):
            # Prefer to break on a sentence / paragraph boundary within overlap window.
            search_start = max(cursor + chunk_chars - overlap, cursor + 1)
            tail = text[search_start:end]
            cut = None
            for pat in ("\n\n", ".\n", "?\n", "!\n", ". ", "\n"):
                idx = tail.rfind(pat)
                if idx != -1:
                    cut = search_start + idx + len(pat)
                    break
            if cut is not None:
                end = cut
        piece = text[cursor:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        cursor = end - overlap
    return chunks


def chunk_page(page: dict, chunk_chars: int, overlap_chars: int) -> list[dict]:
    """Return a list of chunk dicts ready to embed."""
    body = page.get("text") or ""
    title = page.get("title") or page.get("_slug", "")
    page_slug = page.get("_slug", "")
    url = page["url"]
    # Always prefix chunk_text with the page title + URL so the LLM sees them
    # even if it only reads the first ~200 chars of a long chunk.
    header = f"Title: {title}\nSource URL: {url}\n\nContent:\n"
    header_chars = len(header)
    pieces = _split_tokens_approx(
        body, max(chunk_chars - header_chars, 200), max(overlap_chars, 40)
    )
    out = []
    for i, piece in enumerate(pieces):
        out.append(
            {
                "url": url,
                "title": title,
                "page_slug": page_slug,
                "chunk_index_within_page": i,
                "chunk_text": header + piece,
            }
        )
    return out


def _stable_id(chunk: dict) -> str:
    h = hashlib.sha256()
    h.update(chunk["url"].encode())
    h.update(str(chunk.get("chunk_index_within_page", 0)).encode())
    h.update(chunk["chunk_text"].encode("utf-8"))
    return h.hexdigest()[:12]


async def _embed_batch(client, texts: list[str]) -> list[list[float]]:
    # Use dimensions= parameter to guarantee vector size.
    resp = await client.embeddings.create(
        model=EMBEDDING_MODEL, input=texts, dimensions=EMBEDDING_DIM
    )
    # Preserve input order (API returns same order).
    by_idx = {d.index: d.embedding for d in resp.data}
    return [by_idx[i] for i in range(len(texts))]


async def main(argv: Iterable[str]) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk-size", type=int, default=500, help="~tokens per chunk (we use ~4× chars proxy)")
    parser.add_argument("--overlap", type=int, default=120, help="chunk overlap in ~tokens")
    parser.add_argument("--skip-empty", action="store_true", help="Treat 0 raw pages as OK (exit 0).")
    args = parser.parse_args(list(argv))

    chunk_chars = args.chunk_size * 4
    overlap_chars = args.overlap * 4

    pages = list(iter_raw_pages())
    if not pages:
        msg = f"No raw pages found at {RAW_DIR}. Run scripts/scrape_site.py first."
        if args.skip_empty:
            print(f"[build_index] SKIP: {msg}")
            return 0
        print(msg, file=sys.stderr)
        return 2
    print(f"[build_index] {len(pages)} raw pages")

    # Chunk each page.
    all_chunks: list[dict] = []
    for p in pages:
        chs = chunk_page(p, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
        all_chunks.extend(chs)
    print(
        f"[build_index] {len(all_chunks)} chunks "
        f"(chunk_chars={chunk_chars}, overlap_chars={overlap_chars})"
    )

    # OpenAI embeddings client (AsyncOpenAI — shared with the runtime retrieve module).
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print(
            "OPENAI_API_KEY env var is required. Add it to .env or the shell.",
            file=sys.stderr,
        )
        return 3

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)

    # Batch-embed.
    started = time.perf_counter()
    total_tokens = 0
    for start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[start:start + BATCH_SIZE]
        texts = [c["chunk_text"] for c in batch]
        try:
            embeddings = await _embed_batch(client, texts)
        except Exception as exc:
            print(f"[build_index] embedding batch failed at index {start}: {exc!r}")
            raise
        assert len(embeddings) == len(batch), "OpenAI returned wrong embedding count"
        for c, vec in zip(batch, embeddings):
            assert len(vec) == EMBEDDING_DIM, f"wrong dim {len(vec)}"
            c["embedding"] = vec
        total_tokens += sum(len(t) // 4 for t in texts)  # rough
        pct = min(100, int((start + len(batch)) / len(all_chunks) * 100))
        print(
            f"[build_index] embedded {start + len(batch)}/{len(all_chunks)} "
            f"({pct}%) ~{total_tokens:,} input chars"
        )

    # Add final global chunk index + stable id.
    for i, c in enumerate(all_chunks):
        c["chunk_index"] = i
        c["id"] = _stable_id(c)

    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIM,
        "chunk_size_tokens_approx": args.chunk_size,
        "overlap_tokens_approx": args.overlap,
        "page_count": len(pages),
        "chunk_count": len(all_chunks),
        "chunks": all_chunks,
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with INDEX_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    elapsed = int(time.perf_counter() - started)
    size_kb = int(INDEX_PATH.stat().st_size / 1024)
    print(
        f"[build_index] DONE in {elapsed}s → {INDEX_PATH.relative_to(ROOT)} "
        f"({size_kb:,} KB, {len(all_chunks)} chunks across {len(pages)} pages)"
    )
    return 0


if __name__ == "__main__":
    import asyncio

    sys.exit(asyncio.run(main(sys.argv[1:])))
