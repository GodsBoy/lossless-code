"""
lossless-code embedding layer (Phase 2 — Semantic Search).

Provides optional vector embedding for messages and hybrid RRF search.
Every code path is behind a try/import guard — the plugin works without
any embedding dependencies installed.

Provider detection order:
  1. fastembed (local ONNX, no PyTorch) — pip install lossless-code[embed]
  2. openai / anthropic (API-based, requires key + explicit config)
  3. numpy BLOB cosine fallback (no extra install for most users)
  4. None — FTS5-only search remains unchanged

All SQL is delegated to db.py. No raw queries here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Module-level embedder cache — keyed by model name; populated on first use.
# fastembed ONNX cold load costs 500ms-2s; this eliminates repeat loads.
_fastembed_cache: dict[str, "TextEmbedding"] = {}

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def detect_provider(cfg: dict) -> Optional[str]:
    """Detect which embedding provider is available given config.

    Returns: "fastembed" | "openai" | "anthropic" | "numpy" | None
    Only returns a provider if embeddingEnabled is True.
    """
    if not cfg.get("embeddingEnabled", False):
        return None

    provider = cfg.get("embeddingProvider", "local")

    if provider == "local":
        try:
            import fastembed  # noqa: F401
            return "fastembed"
        except ImportError:
            pass
        # numpy fallback: can still store/search BLOBs without fastembed
        try:
            import numpy  # noqa: F401
            return "numpy"
        except ImportError:
            return None

    if provider == "openai":
        try:
            import openai  # noqa: F401
            if os.environ.get("OPENAI_API_KEY"):
                return "openai"
        except ImportError:
            pass
        return None

    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
            if os.environ.get("ANTHROPIC_API_KEY"):
                return "anthropic"
        except ImportError:
            pass
        return None

    return None


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], cfg: dict) -> list[Optional[list[float]]]:
    """Embed a batch of texts. Returns list of float vectors (or None on failure).

    Never raises — failures return None entries for the affected texts.
    """
    provider = detect_provider(cfg)
    model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")

    try:
        if provider == "fastembed":
            return _fastembed_embed(texts, model)
        if provider == "openai":
            return _openai_embed(texts, model)
        if provider == "anthropic":
            return _anthropic_embed(texts, model)
    except Exception:
        return [None] * len(texts)
    # numpy provider: no generation capability — requires pre-existing embeddings
    return [None] * len(texts)


def _fastembed_embed(texts: list[str], model_name: str) -> list[Optional[list[float]]]:
    try:
        from fastembed import TextEmbedding
        if model_name not in _fastembed_cache:
            _fastembed_cache[model_name] = TextEmbedding(model_name=model_name)
        embedder = _fastembed_cache[model_name]
        results = []
        for vec in embedder.embed(texts):
            results.append([float(v) for v in vec])
        return results
    except Exception:
        return [None] * len(texts)


def _openai_embed(texts: list[str], model_name: str, cfg: dict = None) -> list[Optional[list[float]]]:
    try:
        import openai
        base_url = None
        api_key = os.environ.get("OPENAI_API_KEY")
        if cfg:
            base_url = cfg.get("openaiBaseUrl") or os.environ.get("OPENAI_BASE_URL")
            if base_url and not api_key:
                api_key = "not-needed"
        client = openai.OpenAI(base_url=base_url, api_key=api_key) if api_key else openai.OpenAI()
        BATCH = 32
        results: list[Optional[list[float]]] = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            try:
                resp = client.embeddings.create(model=model_name, input=batch)
                for item in resp.data:
                    results.append([float(v) for v in item.embedding])
            except Exception:
                results.extend([None] * len(batch))
        return results
    except Exception:
        return [None] * len(texts)


def _anthropic_embed(texts: list[str], model_name: str) -> list[Optional[list[float]]]:
    """Anthropic embedding via voyage-3 or compatible model."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        BATCH = 32
        results: list[Optional[list[float]]] = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            try:
                resp = client.embeddings.create(model=model_name, input=batch)
                for item in resp.data:
                    results.append([float(v) for v in item.embedding])
            except Exception:
                results.extend([None] * len(batch))
        return results
    except Exception:
        return [None] * len(texts)


# ---------------------------------------------------------------------------
# Vector serialisation helpers
# ---------------------------------------------------------------------------

def vec_to_blob(vec: list[float]) -> bytes:
    """Normalise a float vector and serialise to bytes for BLOB storage."""
    try:
        import numpy as np
        arr = np.array(vec, dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        return arr.tobytes()
    except ImportError:
        import struct
        # Pure-Python fallback (slower, no normalisation guarantee)
        magnitude = sum(v * v for v in vec) ** 0.5
        if magnitude > 0:
            vec = [v / magnitude for v in vec]
        return struct.pack(f"{len(vec)}f", *vec)


def blob_to_vec(raw: bytes) -> list[float]:
    """Deserialise a BLOB back to a float list."""
    try:
        import numpy as np
        arr = np.frombuffer(raw, dtype=np.float32)
        return arr.tolist()
    except ImportError:
        import struct
        n = len(raw) // 4
        return list(struct.unpack(f"{n}f", raw))


# ---------------------------------------------------------------------------
# Indexing pipeline
# ---------------------------------------------------------------------------

def embed_messages_batch(db_conn, cfg: dict, session_id: Optional[str] = None) -> int:
    """Embed un-indexed messages and store results. Returns count of newly embedded.

    db_conn: an open sqlite3 connection (passed to avoid circular import issues
             when called from hook_embed.py which opens its own connection).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as _db

    provider = detect_provider(cfg)
    if provider is None or provider == "numpy":
        return 0

    model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    rows = _db.get_unembed_messages(model, session_id)
    if not rows:
        return 0

    texts = [r["content"] for r in rows]
    vecs = embed_texts(texts, cfg)

    stored = 0
    for row, vec in zip(rows, vecs):
        if vec is None:
            continue
        try:
            blob = vec_to_blob(vec)
            _db.upsert_embedding(_db.get_db(), row["id"], model, blob)
            stored += 1
        except Exception:
            pass

    # Record which model produced the embeddings
    if stored > 0:
        current_cfg = _db.load_config()
        current_cfg["lastEmbeddingModel"] = model
        _db.save_config(current_cfg)

    return stored


def reindex_vault(cfg: dict, force: bool = False, model_override: Optional[str] = None) -> int:
    """Embed all un-indexed messages (or all messages if force=True).

    Returns total number of messages embedded.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as _db

    provider = detect_provider(cfg)
    if provider is None or provider == "numpy":
        print("No embedding provider available. Install fastembed: pip install lossless-code[embed]")
        return 0

    model = model_override or cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    cfg_with_model = {**cfg, "embeddingModel": model}

    if force:
        deleted = _db.delete_embeddings_for_model(model)
        if deleted:
            print(f"Cleared {deleted} existing embeddings for {model}")
        rows = _db.get_all_messages_for_reindex(model)
    else:
        rows = _db.get_unembed_messages(model)

    total = len(rows)
    if total == 0:
        print("All messages already indexed.")
        return 0

    print(f"Embedding {total} messages with {model}...")

    BATCH = 32
    stored = 0
    conn = _db.get_db()
    for i in range(0, total, BATCH):
        batch = rows[i:i + BATCH]
        texts = [r["content"] for r in batch]
        vecs = embed_texts(texts, cfg_with_model)
        for row, vec in zip(batch, vecs):
            if vec is None:
                continue
            try:
                blob = vec_to_blob(vec)
                _db.upsert_embedding(conn, row["id"], model, blob)
                stored += 1
            except Exception:
                pass
        if (i + BATCH) % 320 == 0 or i + BATCH >= total:
            print(f"  {min(i + BATCH, total)}/{total} processed...")

    if stored > 0:
        current_cfg = _db.load_config()
        current_cfg["lastEmbeddingModel"] = model
        _db.save_config(current_cfg)

    print(f"Done. {stored}/{total} messages indexed.")
    return stored


# ---------------------------------------------------------------------------
# Hybrid search (RRF fusion)
# ---------------------------------------------------------------------------

def hybrid_search(query: str, cfg: dict, limit: int = 20) -> dict:
    """Hybrid FTS5 + vector search with Reciprocal Rank Fusion.

    Falls back to FTS5-only when:
    - embeddingEnabled is False
    - No provider available
    - No embeddings stored yet
    - Vector search raises any exception

    Returns dict with "messages", "summaries", and optionally "hybrid": True.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as _db

    fts_messages = _db.search_messages(query, limit=limit)
    fts_summaries = _db.search_summaries(query, limit=limit)

    # Check if vector search can run
    if not cfg.get("embeddingEnabled", False):
        return {"messages": fts_messages, "summaries": fts_summaries}

    model = cfg.get("embeddingModel", "BAAI/bge-small-en-v1.5")
    last_model = cfg.get("lastEmbeddingModel")
    if last_model and last_model != model:
        # Warn but don't block — still do FTS-only
        import sys as _sys
        print(
            f"[lossless] Warning: embeddingModel changed to {model!r} but index was built "
            f"with {last_model!r}. Run `lcc reindex --embeddings` to rebuild.",
            file=_sys.stderr,
        )
        return {"messages": fts_messages, "summaries": fts_summaries}

    provider = detect_provider(cfg)
    if provider is None or provider == "numpy":
        return {"messages": fts_messages, "summaries": fts_summaries}

    # Embed the query
    query_vecs = embed_texts([query], cfg)
    if not query_vecs or query_vecs[0] is None:
        return {"messages": fts_messages, "summaries": fts_summaries}

    query_vec = query_vecs[0]

    # Vector search
    try:
        vec_results = _vector_search_numpy(query_vec, model, limit)
    except Exception:
        return {"messages": fts_messages, "summaries": fts_summaries}

    if not vec_results:
        return {"messages": fts_messages, "summaries": fts_summaries}

    # RRF fusion (k=60, weighted)
    w_fts = float(cfg.get("ftsWeight", 1.0))
    w_vec = float(cfg.get("vectorWeight", 1.0))
    k = 60

    scores: dict[int, float] = {}
    for rank, msg in enumerate(fts_messages, start=1):
        mid = int(msg["id"])
        scores[mid] = scores.get(mid, 0.0) + w_fts / (k + rank)
    for rank, (mid, _sim) in enumerate(vec_results, start=1):
        scores[mid] = scores.get(mid, 0.0) + w_vec / (k + rank)

    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

    # Fetch full rows for any IDs not already in fts_messages
    fts_id_set = {m["id"] for m in fts_messages}
    extra_ids = [mid for mid in top_ids if mid not in fts_id_set]
    extra_msgs = _db.get_messages_by_ids(extra_ids) if extra_ids else []
    msg_map = {m["id"]: m for m in fts_messages + extra_msgs}

    ranked = [msg_map[mid] for mid in top_ids if mid in msg_map]
    return {"messages": ranked, "summaries": fts_summaries, "hybrid": True}


def _vector_search_numpy(query_vec: list[float], model_name: str, limit: int) -> list[tuple[int, float]]:
    """Cosine similarity search over BLOB-stored embeddings using numpy."""
    try:
        import numpy as np
    except ImportError:
        return []

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db as _db

    rows = _db.get_all_embeddings(model_name)
    if not rows:
        return []

    dims = len(query_vec)
    q = np.array(query_vec, dtype=np.float32)
    norm = np.linalg.norm(q)
    if norm == 0:
        return []
    q = q / norm

    scored = []
    for row in rows:
        raw = row["vector"]
        if len(raw) != dims * 4:
            continue
        vec = np.frombuffer(raw, dtype=np.float32)
        sim = float(np.dot(q, vec))
        scored.append((row["message_id"], sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
