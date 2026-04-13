"""FTS5 full-text search over messages and summaries."""

import re

# Characters that FTS5 treats as syntax operators
_FTS5_SPECIAL = re.compile(r'[*?()\"^+\-~:]')


def escape_fts5_query(query: str) -> str:
    """Escape an FTS5 query so special characters don't cause syntax errors.

    Strips FTS5 operators and wraps each remaining word in double quotes
    for exact matching.  Returns empty string if nothing useful remains.
    """
    # Remove special characters
    cleaned = _FTS5_SPECIAL.sub(" ", query)
    # Split into words and quote each one
    words = cleaned.split()
    if not words:
        return ""
    return " ".join(f'"{w}"' for w in words)


def search_messages(query: str, limit: int = 20) -> list[dict]:
    from . import get_db
    escaped = escape_fts5_query(query)
    if not escaped:
        return []
    db = get_db()
    rows = db.execute(
        """SELECT m.*, rank
           FROM messages_fts f
           JOIN messages m ON m.id = f.rowid
           WHERE messages_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (escaped, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_summaries(query: str, limit: int = 20) -> list[dict]:
    from . import get_db
    escaped = escape_fts5_query(query)
    if not escaped:
        return []
    db = get_db()
    rows = db.execute(
        """SELECT s.*, rank
           FROM summaries_fts f
           JOIN summaries s ON s.rowid = f.rowid
           WHERE summaries_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (escaped, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_all(query: str, limit: int = 20) -> dict:
    """Search both messages and summaries, return combined results."""
    return {
        "messages": search_messages(query, limit),
        "summaries": search_summaries(query, limit),
    }


__all__ = [
    "escape_fts5_query",
    "search_messages",
    "search_summaries",
    "search_all",
]
