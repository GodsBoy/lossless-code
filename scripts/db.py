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

CREATE TABLE IF NOT EXISTS dream_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_hash      TEXT NOT NULL,
    scope             TEXT NOT NULL DEFAULT 'project',
    dreamed_at        INTEGER NOT NULL,
    patterns_found    INTEGER DEFAULT 0,
    consolidations    INTEGER DEFAULT 0,
    sessions_analyzed INTEGER DEFAULT 0,
    report_path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_dream_log_project ON dream_log(project_hash);
CREATE INDEX IF NOT EXISTS idx_dream_log_time ON dream_log(dreamed_at);

CREATE INDEX IF NOT EXISTS idx_messages_working_dir
    ON messages(working_dir, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_working_dir
    ON sessions(working_dir, started_at);
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
    # Migration: add consolidated column to summaries (idempotent)
    try:
        _conn.execute("ALTER TABLE summaries ADD COLUMN consolidated INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_depth_consolidated "
        "ON summaries(depth, consolidated)"
    )
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
    "summaryProvider": "anthropic",
    "chunkSize": 20,
    "depthThreshold": 10,
    "incrementalMaxDepth": -1,
    "workingDirFilter": None,
    "autoDream": True,
    "dreamAfterSessions": 5,
    "dreamAfterHours": 24,
    "dreamModel": "claude-haiku-4-5-20251001",
    "dreamTokenBudget": 2000,
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


# ---------------------------------------------------------------------------
# Dream
# ---------------------------------------------------------------------------

import hashlib as _hashlib


def project_hash(working_dir: str) -> str:
    """Deterministic hash for a working directory path (SHA-256, 16 hex chars)."""
    return _hashlib.sha256(os.path.abspath(working_dir).encode()).hexdigest()[:16]


def get_last_dream(project_hash_val: str) -> Optional[dict]:
    """Get the most recent dream_log entry for a project."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM dream_log WHERE project_hash = ? ORDER BY dreamed_at DESC LIMIT 1",
        (project_hash_val,),
    ).fetchone()
    return dict(row) if row else None


def get_messages_since(timestamp: int, working_dir: str = None, limit: int = 5000) -> list[dict]:
    """Get messages created after a given timestamp, optionally filtered by working_dir."""
    db = get_db()
    if working_dir:
        rows = db.execute(
            "SELECT * FROM messages WHERE timestamp > ? AND working_dir = ? ORDER BY timestamp LIMIT ?",
            (timestamp, working_dir, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM messages WHERE timestamp > ? ORDER BY timestamp LIMIT ?",
            (timestamp, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_summaries_since(timestamp: int, working_dir: str = None, limit: int = 2000) -> list[dict]:
    """Get summaries created after a given timestamp, optionally filtered by project working_dir."""
    db = get_db()
    if working_dir:
        rows = db.execute(
            """SELECT s.* FROM summaries s
               JOIN sessions sess ON s.session_id = sess.session_id
               WHERE s.created_at > ? AND sess.working_dir = ?
               ORDER BY s.created_at LIMIT ?""",
            (timestamp, working_dir, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM summaries WHERE created_at > ? ORDER BY created_at LIMIT ?",
            (timestamp, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_overlapping_summaries(depth: int, threshold: float = 0.5) -> list[tuple[str, str]]:
    """Find pairs of summaries at a given depth that share >threshold of their sources.

    Uses a single bulk query instead of N+1 queries, then does pairwise comparison.
    Returns list of (summary_id_a, summary_id_b) pairs.
    """
    db = get_db()
    # Single bulk query: join summaries with their sources
    rows = db.execute(
        """SELECT ss.summary_id, ss.source_id
           FROM summary_sources ss
           JOIN summaries s ON s.id = ss.summary_id
           WHERE s.depth = ? AND s.consolidated = 0""",
        (depth,),
    ).fetchall()

    if not rows:
        return []

    # Build source sets in one pass
    source_sets: dict[str, set[str]] = {}
    for r in rows:
        source_sets.setdefault(r["summary_id"], set()).add(r["source_id"])

    if len(source_sets) < 2:
        return []

    # Find overlapping pairs
    pairs = []
    ids = list(source_sets.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            sa, sb = source_sets[a], source_sets[b]
            if not sa or not sb:
                continue
            overlap = len(sa & sb)
            min_size = min(len(sa), len(sb))
            if min_size > 0 and overlap / min_size > threshold:
                pairs.append((a, b))

    return pairs


def store_dream_log(
    project_hash_val: str,
    scope: str,
    patterns_found: int,
    consolidations: int,
    sessions_analyzed: int,
    report_path: str = "",
    dreamed_at: Optional[int] = None,
) -> int:
    """Record a dream cycle in the log. Returns the new row id."""
    db = get_db()
    now = dreamed_at if dreamed_at is not None else int(time.time())
    cur = db.execute(
        """INSERT INTO dream_log
           (project_hash, scope, dreamed_at, patterns_found, consolidations,
            sessions_analyzed, report_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project_hash_val, scope, now, patterns_found, consolidations,
         sessions_analyzed, report_path),
    )
    db.commit()
    return cur.lastrowid


def mark_consolidated(summary_ids: list[str]) -> None:
    """Mark summaries as consolidated (without deleting them)."""
    db = get_db()
    db.executemany(
        "UPDATE summaries SET consolidated = 1 WHERE id = ?",
        [(sid,) for sid in summary_ids],
    )
    db.commit()


def get_max_summary_depth() -> int:
    """Return the maximum summary depth in the vault."""
    db = get_db()
    row = db.execute("SELECT COALESCE(MAX(depth), 0) FROM summaries").fetchone()
    return row[0] if row else 0


def count_sessions_since(timestamp: int, working_dir: str = None) -> int:
    """Count sessions started after a given timestamp."""
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
