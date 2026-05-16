"""Session lifecycle helpers for the lossless-code vault."""

import fnmatch
import time
from typing import Optional


def matches_any_pattern(session_id: str, patterns: list) -> bool:
    """Return True if session_id matches any fnmatch glob pattern in the list."""
    return any(fnmatch.fnmatch(session_id, p) for p in patterns)


def get_session_stateless(session_id: str) -> bool:
    """Return True if the session exists and is marked stateless."""
    from . import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT stateless FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return bool(row and row[0])


def ensure_session(
    session_id: str,
    working_dir: str = "",
    stateless: bool = False,
    agent_source: str = "claude-code",
) -> None:
    """Create session row if it doesn't exist; update last_active.

    stateless=True marks the session as read-only for summarization purposes —
    messages are stored but dream/summarization passes skip the session.
    """
    from . import get_db
    db = get_db()
    now = int(time.time())
    db.execute(
        """INSERT INTO sessions
           (session_id, working_dir, started_at, last_active, stateless, agent_source)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             last_active = ?,
             agent_source = COALESCE(sessions.agent_source, excluded.agent_source)""",
        (session_id, working_dir, now, now, int(stateless), agent_source, now),
    )
    db.commit()


def get_session(session_id: str) -> Optional[dict]:
    from . import get_db
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 20) -> list[dict]:
    from . import get_db
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sessions ORDER BY last_active DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def set_handoff(session_id: str, text: str) -> None:
    from . import get_db
    db = get_db()
    db.execute(
        "UPDATE sessions SET handoff_text = ? WHERE session_id = ?",
        (text, session_id),
    )
    db.commit()


def count_sessions_since(timestamp: int, working_dir: str = None) -> int:
    """Count sessions started after a given timestamp."""
    from . import get_db
    db = get_db()
    if working_dir:
        row = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at > ? AND working_dir = ?",
            (timestamp, working_dir),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at > ?",
            (timestamp,),
        ).fetchone()
    return row[0] if row else 0


__all__ = [
    "matches_any_pattern",
    "get_session_stateless",
    "ensure_session",
    "get_session",
    "list_sessions",
    "set_handoff",
    "count_sessions_since",
]
