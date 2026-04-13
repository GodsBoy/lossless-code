"""Message embeddings for semantic search (Phase 2)."""

import sqlite3
import time
from typing import Optional


def upsert_embedding(conn: sqlite3.Connection, message_id: int, model_name: str, vector: bytes) -> None:
    """Insert or replace an embedding for a message."""
    now = int(time.time())
    conn.execute(
        """INSERT INTO message_embeddings (message_id, model_name, vector, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(message_id, model_name) DO UPDATE SET vector=excluded.vector, created_at=excluded.created_at""",
        (message_id, model_name, vector, now),
    )
    conn.commit()


def get_unembed_messages(model_name: str, session_id: Optional[str] = None) -> list[dict]:
    """Return messages that have no embedding for the given model."""
    from . import get_db
    db = get_db()
    if session_id:
        rows = db.execute(
            """SELECT m.id, m.content FROM messages m
               LEFT JOIN message_embeddings e ON e.message_id = m.id AND e.model_name = ?
               WHERE e.id IS NULL AND m.session_id = ?
               ORDER BY m.timestamp""",
            (model_name, session_id),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT m.id, m.content FROM messages m
               LEFT JOIN message_embeddings e ON e.message_id = m.id AND e.model_name = ?
               WHERE e.id IS NULL
               ORDER BY m.timestamp""",
            (model_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_messages_for_reindex(model_name: str) -> list[dict]:
    """Return all messages, including those with existing embeddings (for --force reindex)."""
    from . import get_db
    db = get_db()
    rows = db.execute(
        "SELECT id, content FROM messages ORDER BY timestamp"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_embeddings_for_model(model_name: str) -> int:
    """Delete all embeddings for a specific model (used by --force reindex)."""
    from . import get_db
    db = get_db()
    cur = db.execute(
        "DELETE FROM message_embeddings WHERE model_name = ?", (model_name,)
    )
    db.commit()
    return cur.rowcount


def count_embeddings(model_name: Optional[str] = None) -> int:
    """Count embeddings, optionally filtered by model."""
    from . import get_db
    db = get_db()
    if model_name:
        row = db.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model_name = ?", (model_name,)
        ).fetchone()
    else:
        row = db.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()
    return row[0] if row else 0


def get_embedding_model_coverage(model_name: str) -> dict:
    """Return coverage stats for a model: total messages, embedded, pending."""
    from . import get_db
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    embedded = db.execute(
        "SELECT COUNT(*) FROM message_embeddings WHERE model_name = ?", (model_name,)
    ).fetchone()[0]
    return {"total": total, "embedded": embedded, "pending": total - embedded}


def get_all_embeddings(model_name: str) -> list[dict]:
    """Return all (message_id, vector) pairs for a model — used by numpy fallback search."""
    from . import get_db
    db = get_db()
    rows = db.execute(
        "SELECT message_id, vector FROM message_embeddings WHERE model_name = ?",
        (model_name,),
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "upsert_embedding",
    "get_unembed_messages",
    "get_all_messages_for_reindex",
    "delete_embeddings_for_model",
    "count_embeddings",
    "get_embedding_model_coverage",
    "get_all_embeddings",
]
