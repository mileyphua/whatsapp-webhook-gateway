"""Runtime vector retrieval for Petrobind's scraped knowledge base.

Architecture (aligned with PLAN.md Part 4):
  - At startup, `load_index_if_needed()` reads rag/index.json into memory
    (list of {url, title, chunk_text, embedding}).
  - For each question, `retrieve(query)`:
      1. Embeds the user query with OpenAI text-embedding-3-small.
      2. Computes cosine similarity against every loaded chunk vector.
      3. Returns the top-k chunks above a configurable minimum similarity.
  - If no chunks clear the similarity threshold -> returns empty list, which
    signals the caller (llm_assistant) to short-circuit to the handoff tool
    instead of letting the LLM guess.

No external vector DB needed at this scale (~72 pages, few hundred vectors).
Numpy handles cosine similarity as a single matmul — sub-millisecond.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv():  # type: ignore[misc]
        return False

load_dotenv()  # ensure OPENAI_API_KEY etc are available from .env if present

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is pinned in requirements
    np = None  # type: ignore[assignment]

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "rag" / "index.json"

MIN_SIMILARITY = 0.36  # tuned via PLAN Part 4.7 tests; raise if hallucinations occur
DEFAULT_TOP_K = 4


@dataclass(frozen=True)
class RetrievedChunk:
    url: str
    title: str
    chunk_text: str
    similarity: float
    chunk_index: int


_index: list[dict] | None = None
_vectors: "np.ndarray | None" = None


def index_is_ready() -> bool:
    return _index is not None and _vectors is not None and np is not None


def _openai_embeddings_client():
    # Imported lazily so this module can be imported without the API key set
    # (e.g. during build_index.py, which uses the client directly).
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY env var is required for embeddings retrieval. "
            "Set it in .env (local) or Render Environment (deployed)."
        )
    return AsyncOpenAI(api_key=api_key)


def load_index_if_needed(*, force: bool = False) -> tuple[list[dict], "np.ndarray | None"]:
    global _index, _vectors

    if _index is not None and not force:
        assert _vectors is not None or len(_index) == 0
        return _index, _vectors

    if not INDEX_PATH.exists():
        print(f"[RAG] index not yet built at {INDEX_PATH.relative_to(ROOT)}")
        _index = []
        _vectors = None
        return _index, _vectors

    with INDEX_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    entries: list[dict] = raw.get("chunks", []) if isinstance(raw, dict) else list(raw)
    if not entries or np is None:
        _index = entries
        _vectors = None
        print(f"[RAG] loaded {len(entries)} chunks (numpy? {np is not None})")
        return _index, _vectors

    vecs = np.zeros((len(entries), EMBEDDING_DIM), dtype=np.float32)
    ok = 0
    for i, c in enumerate(entries):
        v = c.get("embedding") or []
        if len(v) == EMBEDDING_DIM:
            vecs[i] = np.asarray(v, dtype=np.float32)
            ok += 1
    print(f"[RAG] loaded {len(entries)} chunks ({ok} with embeddings) from index.json")
    _index = entries
    _vectors = vecs
    return _index, _vectors


def _cosine(query_vec: "np.ndarray") -> "np.ndarray":
    # query_vec shape: (EMBEDDING_DIM,) ; _vectors shape: (N, EMBEDDING_DIM)
    assert _vectors is not None and query_vec.shape == (EMBEDDING_DIM,)
    dot = _vectors.astype(np.float32) @ query_vec.astype(np.float32)  # (N,)
    q_norm = max(float(np.linalg.norm(query_vec)), 1e-12)
    row_norms = np.linalg.norm(_vectors, axis=1).astype(np.float32)
    denom = np.maximum(row_norms * q_norm, 1e-12)
    return dot / denom


async def retrieve(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = MIN_SIMILARITY,
) -> List[RetrievedChunk]:
    """Return top-k semantically-similar chunks for a user query.

    Returns empty list if:
      - Index has not been built (rag/index.json missing)
      - Embeddings couldn't be computed (e.g. missing OPENAI_API_KEY)
      - No chunk cleared min_similarity threshold
    """
    load_index_if_needed()
    if not _index or _vectors is None or not query.strip():
        return []

    try:
        client = _openai_embeddings_client()
        resp = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=query.strip(),
            dimensions=EMBEDDING_DIM,
        )
        q = np.asarray(resp.data[0].embedding, dtype=np.float32)
    except Exception as exc:
        print(f"[RAG] embeddings call failed: {exc!r}")
        return []

    try:
        sims = _cosine(q)
    except Exception as exc:
        print(f"[RAG] similarity compute failed: {exc!r}")
        return []

    order = np.argsort(-sims)  # descending
    out: List[RetrievedChunk] = []
    for pos in order.tolist():
        s = float(sims[pos])
        if s < min_similarity:
            break
        c = _index[pos]
        out.append(
            RetrievedChunk(
                url=c.get("url", ""),
                title=c.get("title", ""),
                chunk_text=c.get("chunk_text", ""),
                similarity=s,
                chunk_index=int(pos),
            )
        )
        if len(out) >= top_k:
            break
    if out:
        top_sim = f"{out[0].similarity:.3f}"
        print(
            f"[RAG] query={query[:80]!r} hits={len(out)} "
            f"top_sim={top_sim} sources=[{', '.join(c.title[:30] for c in out[:2])}...]"
        )
    else:
        print(f"[RAG] query={query[:80]!r} NO HITS above threshold {min_similarity}")
    return out
