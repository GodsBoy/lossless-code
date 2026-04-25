"""
lossless-code database layer.

Manages the SQLite vault (vault.db) — sessions, messages, summaries, and the
DAG link table (summary_sources).  Every public function in this package
operates on a single connection obtained via ``get_db()``.

Path constants (``LOSSLESS_HOME``, ``VAULT_DIR``, ``VAULT_DB``, ``CONFIG_PATH``)
and the connection state (``_conn``, ``get_db``, ``close_db``) live in this
module rather than a submodule so that test fixtures and the MCP server can
reassign them at runtime via ``db.VAULT_DB = ...``. Submodules look up these
attributes lazily via ``from . import get_db`` inside function bodies.
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

from .schema import SCHEMA_SQL, FTS_SQL, FTS_TRIGGERS_SQL

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
# Connection
# ---------------------------------------------------------------------------

_conn: Optional[sqlite3.Connection] = None


def get_db() -> sqlite3.Connection:
    """Return a module-level connection, creating vault.db if needed."""
    global _conn
    if _conn is not None:
        return _conn

    VAULT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Tighten permissions on existing dir too. mkdir(mode=) only applies to new dirs.
    try:
        os.chmod(VAULT_DIR, 0o700)
    except OSError:
        pass  # Best-effort on shared or readonly mounts.
    _conn = sqlite3.connect(str(VAULT_DB), timeout=10)
    _conn.row_factory = sqlite3.Row
    # Lock down vault.db. The vault contains every captured turn (credentials,
    # file paths, transcripts), so default 0644 is unsafe on shared machines.
    # Idempotent on every connect: cheap, defensive against umask changes.
    try:
        os.chmod(VAULT_DB, 0o600)
    except OSError:
        pass  # File may not exist yet on some platforms; sqlite3.connect creates it.
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
    # Migration: add stateless column to sessions (idempotent)
    try:
        _conn.execute("ALTER TABLE sessions ADD COLUMN stateless INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migration: add file_path column to messages (fingerprint file context)
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN file_path TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migration: add kind column to summaries (polarity classification)
    try:
        _conn.execute("ALTER TABLE summaries ADD COLUMN kind TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migration (v1.2 U1): OTel span columns on messages. NULL-able for backfill
    # compatibility; pre-migration rows have no causal data and that is accepted.
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN parent_message_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN span_kind TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        _conn.execute("ALTER TABLE messages ADD COLUMN attributes TEXT")
    except sqlite3.OperationalError:
        pass
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_file_path "
        "ON messages(file_path) WHERE file_path IS NOT NULL"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summary_sources_source "
        "ON summary_sources(source_type, source_id)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_depth_consolidated "
        "ON summaries(depth, consolidated)"
    )
    # Indexes for span queries (U2 helpers + lcc_expand span-id lookups)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_span_kind "
        "ON messages(span_kind) WHERE span_kind IS NOT NULL"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_parent_id "
        "ON messages(parent_message_id) WHERE parent_message_id IS NOT NULL"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_tool_call_id "
        "ON messages(tool_call_id) WHERE tool_call_id IS NOT NULL"
    )
    # Contracts table (v1.2 U5): typed retractable rules that ride inside
    # the SessionStart bundle. Append-only with supersedes_id chain.
    # IF NOT EXISTS is self-idempotent; no try/except needed.
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS contracts (
            id                 TEXT PRIMARY KEY,
            kind               TEXT NOT NULL,
            body               TEXT NOT NULL,
            byline_session_id  TEXT,
            byline_model       TEXT,
            created_at         INTEGER NOT NULL,
            status             TEXT NOT NULL DEFAULT 'Pending',
            supersedes_id      TEXT,
            scope              TEXT DEFAULT 'project',
            body_hash          TEXT
        )"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contracts_status "
        "ON contracts(status)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contracts_supersedes "
        "ON contracts(supersedes_id) WHERE supersedes_id IS NOT NULL"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contracts_body_hash "
        "ON contracts(body_hash) WHERE body_hash IS NOT NULL"
    )
    # Migration: message_embeddings table for semantic search (IF NOT EXISTS
    # is self-idempotent — no try/except needed, and a bare except would mask
    # unrelated OperationalError from index creation).
    _conn.execute(
        """CREATE TABLE IF NOT EXISTS message_embeddings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            model_name TEXT NOT NULL,
            vector     BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(message_id, model_name)
        )"""
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_emb_message "
        "ON message_embeddings(message_id)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_emb_model "
        "ON message_embeddings(model_name)"
    )
    _conn.commit()
    return _conn


def close_db() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---------------------------------------------------------------------------
# Submodule re-exports (flat namespace for backward compatibility)
# ---------------------------------------------------------------------------
#
# Submodules are imported AFTER get_db is defined above, because each
# submodule uses ``from . import get_db`` at call time.  The lazy lookup
# pattern means the reverse is also safe — the submodule modules import
# cleanly even if get_db is not yet bound when they parse, but our import
# order guarantees it is bound before they ever execute a call.

from .config import DEFAULT_CONFIG, load_config, save_config
from .sessions import (
    matches_any_pattern,
    get_session_stateless,
    ensure_session,
    get_session,
    list_sessions,
    set_handoff,
    count_sessions_since,
)
from .messages import (
    store_message,
    get_unsummarised,
    mark_summarised,
    get_messages_by_ids,
    count_session_messages,
    get_messages_since,
)
from .summaries import (
    gen_summary_id,
    store_summary,
    get_summary,
    get_summary_sources,
    get_summaries_at_depth,
    get_top_summaries,
    get_summaries_since,
    get_summary_ids_since,
    get_summaries_by_ids,
    get_overlapping_summaries,
    mark_consolidated,
    get_summaries_for_file,
    get_max_summary_depth,
)
from .search import escape_fts5_query, search_messages, search_summaries, search_all
from .dream_log import project_hash, get_last_dream, store_dream_log
from .spans import (
    get_span,
    get_span_chain,
    get_children_spans,
    cap_attributes_json,
)
from .contracts import (
    gen_contract_id,
    store_contract_candidate,
    get_contract,
    list_contracts,
    approve_contract,
    reject_contract,
    retract_contract,
    supersede_contract,
)
from .embeddings import (
    upsert_embedding,
    get_unembed_messages,
    get_all_messages_for_reindex,
    delete_embeddings_for_model,
    count_embeddings,
    get_embedding_model_coverage,
    get_all_embeddings,
)

__all__ = [
    # Path constants and connection
    "LOSSLESS_HOME",
    "VAULT_DIR",
    "VAULT_DB",
    "CONFIG_PATH",
    "get_db",
    "close_db",
    # Schema constants
    "SCHEMA_SQL",
    "FTS_SQL",
    "FTS_TRIGGERS_SQL",
    # Config
    "DEFAULT_CONFIG",
    "load_config",
    "save_config",
    # Sessions
    "matches_any_pattern",
    "get_session_stateless",
    "ensure_session",
    "get_session",
    "list_sessions",
    "set_handoff",
    "count_sessions_since",
    # Messages
    "store_message",
    "get_unsummarised",
    "mark_summarised",
    "get_messages_by_ids",
    "count_session_messages",
    "get_messages_since",
    # Summaries / DAG
    "gen_summary_id",
    "store_summary",
    "get_summary",
    "get_summary_sources",
    "get_summaries_at_depth",
    "get_top_summaries",
    "get_summaries_since",
    "get_summary_ids_since",
    "get_summaries_by_ids",
    "get_overlapping_summaries",
    "mark_consolidated",
    "get_summaries_for_file",
    "get_max_summary_depth",
    # Search
    "escape_fts5_query",
    "search_messages",
    "search_summaries",
    "search_all",
    # Dream log
    "project_hash",
    "get_last_dream",
    "store_dream_log",
    # Spans (v1.2 U2): OTel-shaped messages graph
    "get_span",
    "get_span_chain",
    "get_children_spans",
    "cap_attributes_json",
    # Contracts (v1.2 U5): typed retractable rules
    "gen_contract_id",
    "store_contract_candidate",
    "get_contract",
    "list_contracts",
    "approve_contract",
    "reject_contract",
    "retract_contract",
    "supersede_contract",
    # Embeddings
    "upsert_embedding",
    "get_unembed_messages",
    "get_all_messages_for_reindex",
    "delete_embeddings_for_model",
    "count_embeddings",
    "get_embedding_model_coverage",
    "get_all_embeddings",
]
