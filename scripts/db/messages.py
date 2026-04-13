"""Message storage and retrieval for the lossless-code vault."""

import time
from typing import Optional


def store_message(
    session_id: str,
    role: str,
    content: str,
    turn_id: str = "",
    tool_name: str = "",
    working_dir: str = "",
    file_path: Optional[str] = None,
) -> int:
    """Insert a message and return its id.

    ``file_path`` tags the message with the file that a tool call touched
    (Read/Edit/Write/MultiEdit/NotebookEdit). Repo-relative when the path
    lies under ``working_dir``, absolute otherwise. None for non-file messages.
    """
    from . import get_db
    db = get_db()
    now = int(time.time())
    cur = db.execute(
        """INSERT INTO messages
           (session_id, turn_id, role, content, tool_name, working_dir, timestamp, file_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, turn_id, role, content, tool_name, working_dir, now, file_path),
    )
    db.commit()
    return cur.lastrowid


def get_unsummarised(session_id: Optional[str] = None) -> list[dict]:
    """Get messages not yet summarised, optionally filtered by session.

    Always excludes messages from stateless sessions (mirrors get_messages_since).
    """
    from . import get_db
    db = get_db()
    if session_id:
        rows = db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN sessions s ON m.session_id = s.session_id
               WHERE m.summarised = 0 AND m.session_id = ?
                 AND (s.stateless IS NULL OR s.stateless = 0)
               ORDER BY m.timestamp""",
            (session_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN sessions s ON m.session_id = s.session_id
               WHERE m.summarised = 0
                 AND (s.stateless IS NULL OR s.stateless = 0)
               ORDER BY m.timestamp"""
        ).fetchall()
    return [dict(r) for r in rows]


def mark_summarised(message_ids: list[int]) -> None:
    from . import get_db
    db = get_db()
    db.executemany(
        "UPDATE messages SET summarised = 1 WHERE id = ?",
        [(mid,) for mid in message_ids],
    )
    db.commit()


def get_messages_by_ids(ids: list) -> list[dict]:
    from . import get_db
    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY timestamp",
        [str(i) for i in ids],
    ).fetchall()
    return [dict(r) for r in rows]


def count_session_messages(session_id: str) -> int:
    """Count messages already stored for a given session."""
    from . import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else 0


def get_messages_since(timestamp: int, working_dir: str = None, limit: int = 5000) -> list[dict]:
    """Get messages created after a given timestamp, optionally filtered by working_dir.

    Always excludes messages from stateless sessions (e.g. subagent/cron sessions).
    """
    from . import get_db
    db = get_db()
    # LEFT JOIN guards against messages with no matching session row
    if working_dir:
        rows = db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN sessions s ON m.session_id = s.session_id
               WHERE m.timestamp > ? AND m.working_dir = ?
                 AND (s.stateless IS NULL OR s.stateless = 0)
               ORDER BY m.timestamp LIMIT ?""",
            (timestamp, working_dir, limit),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN sessions s ON m.session_id = s.session_id
               WHERE m.timestamp > ?
                 AND (s.stateless IS NULL OR s.stateless = 0)
               ORDER BY m.timestamp LIMIT ?""",
            (timestamp, limit),
        ).fetchall()
    return [dict(r) for r in rows]


__all__ = [
    "store_message",
    "get_unsummarised",
    "mark_summarised",
    "get_messages_by_ids",
    "count_session_messages",
    "get_messages_since",
]
