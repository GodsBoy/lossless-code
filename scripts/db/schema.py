"""SQL constants for the lossless-code vault schema."""

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    working_dir  TEXT,
    started_at   INTEGER,
    last_active  INTEGER,
    handoff_text TEXT,
    stateless    INTEGER NOT NULL DEFAULT 0,
    agent_source TEXT DEFAULT 'claude-code'
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
    agent_source TEXT DEFAULT 'claude-code',
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

CREATE TABLE IF NOT EXISTS imported_task_state (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_root      TEXT NOT NULL,
    source_runtime    TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_timestamp  INTEGER,
    source_pointer    TEXT,
    goal              TEXT,
    last_step         TEXT,
    next_step         TEXT,
    blockers          TEXT,
    confidence        TEXT NOT NULL DEFAULT 'low',
    status            TEXT NOT NULL DEFAULT 'partial',
    warning           TEXT,
    imported_at       INTEGER NOT NULL,
    UNIQUE(project_root, source_runtime, source_session_id)
);
CREATE INDEX IF NOT EXISTS idx_imported_task_state_project
    ON imported_task_state(project_root, source_runtime, source_timestamp, imported_at);
"""

FTS_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content=messages, content_rowid=id);

CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts
    USING fts5(content, content=summaries, content_rowid=rowid);
"""

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

__all__ = ["SCHEMA_SQL", "FTS_SQL", "FTS_TRIGGERS_SQL"]
