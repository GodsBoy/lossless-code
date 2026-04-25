"""Span-graph queries over the OTel-shaped messages table.

The v1.2 substrate adds parent_message_id, span_kind, tool_call_id, attributes
columns to messages (see U1). This module exposes the queries that consumers
(U4 lcc_expand span-id resolution, U10 bundle assembler) need.

Naming follows the package convention (sessions.py, messages.py, summaries.py,
dream_log.py): pure functions, lazy ``from . import get_db`` inside function
bodies, no module-level state. New public names are re-exported from
scripts/db/__init__.py and listed in __all__.
"""

import json
from typing import Optional


def get_span(message_id: int) -> Optional[dict]:
    """Fetch a single message row by id, with attributes JSON decoded.

    Returns None when no row matches. Caller decides whether absence is a
    structured error (lcc_expand returns span_not_found) or a quiet None
    (bundle assembler skips the slot).
    """
    from . import get_db

    db = get_db()
    row = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    attrs_raw = out.get("attributes")
    if attrs_raw:
        try:
            out["attributes"] = json.loads(attrs_raw)
        except (json.JSONDecodeError, ValueError):
            # Malformed JSON in storage. Surface the raw string rather than
            # raising; the caller can decide whether to repair or warn.
            out["attributes"] = {"_invalid_json": attrs_raw}
    else:
        out["attributes"] = None
    return out


def get_span_chain(message_id: int, max_hops: int = 32) -> list[dict]:
    """Walk parent_message_id upward from the given message to the root.

    Returns rows ordered leaf-first (the seed message is index 0; its parent
    is index 1, and so on). Always returns at least one row for an existing
    message_id; an empty list signals the message_id was not found.

    The recursive CTE caps at ``max_hops`` to prevent runaway walks if a
    cycle ever lands in the table (FK semantics forbid cycles, but a manual
    SQL edit could create one). Default 32 hops is well above any realistic
    conversation depth.

    Mirrors the recursive-CTE pattern in summaries.get_summaries_for_file
    (hop-bounded, leaf-seeded, parent-edge-walked).
    """
    from . import get_db

    db = get_db()
    rows = db.execute(
        """
        WITH RECURSIVE ancestors(id, hop) AS (
            SELECT id, 0
            FROM messages
            WHERE id = ?

            UNION

            SELECT m.id, a.hop + 1
            FROM ancestors a
            JOIN messages m ON m.id = (
                SELECT parent_message_id FROM messages WHERE id = a.id
            )
            WHERE a.hop < ?
              AND m.id IS NOT NULL
        )
        SELECT m.*, a.hop
        FROM ancestors a
        JOIN messages m ON m.id = a.id
        ORDER BY a.hop ASC
        """,
        (message_id, max_hops),
    ).fetchall()
    return [_decode_attrs(dict(r)) for r in rows]


def get_children_spans(
    parent_id: int, span_kind: Optional[str] = None
) -> list[dict]:
    """Direct children of ``parent_id``. Optionally filter by span_kind.

    One-hop only. Use get_span_chain for multi-hop walks (the inverse
    direction is rarely needed; YAGNI'd for v1.2).
    """
    from . import get_db

    db = get_db()
    if span_kind is not None:
        rows = db.execute(
            "SELECT * FROM messages WHERE parent_message_id = ? AND span_kind = ? "
            "ORDER BY timestamp ASC",
            (parent_id, span_kind),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM messages WHERE parent_message_id = ? ORDER BY timestamp ASC",
            (parent_id,),
        ).fetchall()
    return [_decode_attrs(dict(r)) for r in rows]


def cap_attributes_json(attrs: Optional[dict], max_tokens: int = 500) -> str:
    """Serialize an attributes dict to JSON, capping size at write time.

    The attributes column is TEXT storing JSON. Without a write-time cap,
    a crafted hook payload could store unboundedly large JSON and inflate
    vault.db (see vault-db-unbounded-growth-oom.md). Mirrors the discipline
    in summarise.cap_summary_text.

    Cap policy:
    - None or non-dict input returns "{}" (per U3 shape-validation contract).
    - Dict serializes via json.dumps with compact separators.
    - When the serialized payload exceeds max_tokens (estimated at 4 chars per
      token), replace the entire payload with a tombstone preserving the size
      signal: ``{"_capped": true, "_original_chars": N}``. Valid JSON, tiny,
      and surfaces the cap event to anyone reading the column.
    """
    if not isinstance(attrs, dict):
        return "{}"
    payload = json.dumps(attrs, separators=(",", ":"))
    max_chars = max_tokens * 4
    if len(payload) <= max_chars:
        return payload
    return json.dumps(
        {"_capped": True, "_original_chars": len(payload)},
        separators=(",", ":"),
    )


def _decode_attrs(row: dict) -> dict:
    """Decode the attributes JSON in-place for a row dict. Internal helper."""
    raw = row.get("attributes")
    if raw:
        try:
            row["attributes"] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            row["attributes"] = {"_invalid_json": raw}
    else:
        row["attributes"] = None
    return row


__all__ = [
    "get_span",
    "get_span_chain",
    "get_children_spans",
    "cap_attributes_json",
]
