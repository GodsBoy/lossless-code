"""
lossless-code database layer.

Manages the SQLite vault (vault.db) — sessions, messages, summaries, and the
DAG link table (summary_sources).  Every public function in this module
operates on a single connection obtained via `get_db()`.
"""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# LOSSLESS_HOME: where scripts/hooks live (may be plugin cache dir)
# LOSSLESS_VAULT_DIR: where vault.db persists (always ~/.lossless-code)
LOSSLESS_HOME = Path(os.environ.get("LOSSLESS_HOME", Path.home() / ".lossless-code"))
VAULT_DIR = Path(os.environ.get("LOSSLESS_VAULT_DIR", Path.home() / ".lossless-code"))
VAULT_DB = VAULT_DIR / "vault.db"
CONFIG_PATH = VAULT_DIR / "config.json"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    working_dir  TEXT,
    started_at   INTEGER,
    last_active  INTEGER,
    handoff_text TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    turn_id     TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tool_name   TEXT,
    working_dir TEXT,
    timestamp   INTEGER NOT NULL,
    summarised  INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    content     TEXT NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    token_count INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_sources (
    summary_id  TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    FOREIGN KEY (summary_id) REFERENCES summaries(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_unsummarised ON messages(summarised, timestamp);
CREATE INDEX IF NOT EXISTS idx_summaries_session  ON summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_summaries_depth    ON summaries(depth);
CREATE INDEX IF NOT EXISTS idx_summary_sources_id ON summary_sources(summary_id);
"""

FTS_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content=messages, content_rowid=id);

CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts
    USING fts5(content, content=summaries, content_rowid=rowid);
"""

# Triggers to keep FTS in sync with base tables
FTS_TRIGGERS_SQL = """\
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, content)
        VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS summaries_au AFTER UPDATE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, content)
        VALUES('delete', old.rowid, old.content);
    INSERT INTO summaries_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    """Return a module-level connection, creating vault.db if needed."""
    global _conn
    if _conn is not None:
        return _conn

    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(VAULT_DB), timeout=10)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA_SQL)
    _conn.executescript(FTS_SQL)
    _conn.executescript(FTS_TRIGGERS_SQL)
    _conn.commit()
    return _conn


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "summaryModel": "claude-haiku-4-5-20251001",
    "chunkSize": 20,
    "depthThreshold": 10,
    "incrementalMaxDepth": -1,
    "workingDirFilter": None,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **user_cfg}
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def ensure_session(session_id: str, working_dir: str = "") -> None:
    """Create session row if it doesn't exist; update last_active."""
    db = get_db()
    now = int(time.time())
    db.execute(
        """INSERT INTO sessions (session_id, working_dir, started_at, last_active)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET last_active = ?""",
        (session_id, working_dir, now, now, now),
    )
    db.commit()


def get_session(session_id: str) -> Optional[dict]:
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 20) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sessions ORDER BY last_active DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def set_handoff(session_id: str, text: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE sessions SET handoff_text = ? WHERE session_id = ?",
        (text, session_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def store_message(
    session_id: str,
    role: str,
    content: str,
    turn_id: str = "",
    tool_name: str = "",
    working_dir: str = "",
) -> int:
    """Insert a message and return its id."""
    db = get_db()
    now = int(time.time())
    cur = db.execute(
        """INSERT INTO messages
           (session_id, turn_id, role, content, tool_name, working_dir, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, turn_id, role, content, tool_name, working_dir, now),
    )
    db.commit()
    return cur.lastrowid


def get_unsummarised(session_id: Optional[str] = None) -> list[dict]:
    """Get messages not yet summarised, optionally filtered by session."""
    db = get_db()
    if session_id:
        rows = db.execute(
            """SELECT * FROM messages
               WHERE summarised = 0 AND session_id = ?
               ORDER BY timestamp""",
            (session_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM messages WHERE summarised = 0 ORDER BY timestamp"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_summarised(message_ids: list[int]) -> None:
    db = get_db()
    db.executemany(
        "UPDATE messages SET summarised = 1 WHERE id = ?",
        [(mid,) for mid in message_ids],
    )
    db.commit()


def get_messages_by_ids(ids: list) -> list[dict]:
    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY timestamp",
        [str(i) for i in ids],
    ).fetchall()
    return [dict(r) for r in rows]


def count_session_messages(session_id: str) -> int:
    """Count messages already stored for a given session."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Summaries & DAG
# ---------------------------------------------------------------------------

def gen_summary_id() -> str:
    return f"sum_{uuid.uuid4().hex[:12]}"


def store_summary(
    summary_id: str,
    content: str,
    depth: int,
    source_ids: list[tuple[str, str]],
    session_id: Optional[str] = None,
    token_count: Optional[int] = None,
) -> None:
    """
    Write a summary node and its source links.

    source_ids: list of (source_type, source_id) — e.g. ('message', '42')
    """
    db = get_db()
    now = int(time.time())
    db.execute(
        """INSERT INTO summaries (id, session_id, content, depth, token_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (summary_id, session_id, content, depth, token_count, now),
    )
    db.executemany(
        "INSERT INTO summary_sources (summary_id, source_type, source_id) VALUES (?, ?, ?)",
        [(summary_id, stype, sid) for stype, sid in source_ids],
    )
    db.commit()


def get_summary(summary_id: str) -> Optional[dict]:
    db = get_db()
    row = db.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
    return dict(row) if row else None


def get_summary_sources(summary_id: str) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM summary_sources WHERE summary_id = ?", (summary_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_summaries_at_depth(depth: int, session_id: Optional[str] = None) -> list[dict]:
    db = get_db()
    if session_id:
        rows = db.execute(
            "SELECT * FROM summaries WHERE depth = ? AND session_id = ? ORDER BY created_at",
            (depth, session_id),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM summaries WHERE depth = ? ORDER BY created_at", (depth,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_top_summaries(limit: int = 5, session_id: Optional[str] = None) -> list[dict]:
    """Get the highest-depth (most compressed) summaries."""
    db = get_db()
    if session_id:
        rows = db.execute(
            """SELECT * FROM summaries WHERE session_id = ?
               ORDER BY depth DESC, created_at DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM summaries ORDER BY depth DESC, created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------------

import re as _re

# Characters that FTS5 treats as syntax operators
_FTS5_SPECIAL = _re.compile(r'[*?()\"^+\-~:]')


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
