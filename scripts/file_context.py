#!/usr/bin/env python3
"""
File context fingerprint lookup for the PreToolUse hook.

Fast path: read the JSON cache under ``${LOSSLESS_HOME}/cache/``.
Cold path: open vault.db read-only, run ``get_summaries_for_file``,
render via ``format_file_fingerprint``, write the result back to cache.

Performance targets (from plan SC4):
- Warm ≤ 20ms p95
- Cold ≤ 200ms p95
- Default-off via ``fileContextEnabled``; returns empty output when disabled

Concurrency:
- Cache reads use ``fcntl.flock(LOCK_SH | LOCK_NB)``
- Cache writes use ``LOCK_EX | LOCK_NB`` + atomic ``tempfile + os.replace``
- Single-flight stampede guard via ``O_CREAT | O_EXCL`` sentinel files
- SQLite opened in read-only URI mode with ``PRAGMA busy_timeout = 100``

Gated on ``fileContextEnabled`` (default off) so the hook adds zero
latency until the flag flips.
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from inject_context import format_file_fingerprint

CACHE_TTL_SECONDS = 60
DEFAULT_LIMIT = 3


def _cache_dir() -> Path:
    return db.VAULT_DIR / "cache"


def _cache_file() -> Path:
    return _cache_dir() / "file_fingerprints.json"


def _inflight_dir() -> Path:
    return _cache_dir() / "inflight"


def _inflight_sentinel(file_path: str) -> Path:
    digest = hashlib.sha1(file_path.encode("utf-8")).hexdigest()
    return _inflight_dir() / digest


def _ensure_cache_dirs() -> None:
    _cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    _inflight_dir().mkdir(parents=True, exist_ok=True, mode=0o700)


def _load_cache() -> dict:
    path = _cache_file()
    if not path.exists():
        return {}
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return {}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (OSError, IOError):
            return {}
        try:
            raw = os.read(fd, 1024 * 1024)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _store_cache(cache: dict) -> None:
    path = _cache_file()
    _ensure_cache_dirs()
    # Atomic write: tempfile + os.replace.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".fingerprints.", suffix=".json", dir=str(_cache_dir())
    )
    try:
        os.write(tmp_fd, json.dumps(cache).encode("utf-8"))
    finally:
        os.close(tmp_fd)
    os.chmod(tmp_path, 0o600)

    # Take an exclusive lock on the real file (if it exists) to serialize writers.
    if path.exists():
        try:
            real_fd = os.open(str(path), os.O_WRONLY)
            try:
                try:
                    fcntl.flock(real_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, IOError):
                    os.unlink(tmp_path)
                    return
                os.replace(tmp_path, str(path))
            finally:
                fcntl.flock(real_fd, fcntl.LOCK_UN)
                os.close(real_fd)
            return
        except OSError:
            pass
    os.replace(tmp_path, str(path))


def _claim_inflight(file_path: str) -> int | None:
    """Single-flight guard via O_CREAT|O_EXCL sentinel file.

    Returns the sentinel fd on success, ``None`` if another process holds it.
    """
    _ensure_cache_dirs()
    sentinel = _inflight_sentinel(file_path)
    try:
        fd = os.open(
            str(sentinel),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        return fd
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            # Stale sentinel older than 30s is considered abandoned.
            try:
                age = time.time() - sentinel.stat().st_mtime
                if age > 30:
                    sentinel.unlink(missing_ok=True)
                    return _claim_inflight(file_path)
            except OSError:
                pass
            return None
        return None


def _release_inflight(fd: int, file_path: str) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        _inflight_sentinel(file_path).unlink(missing_ok=True)
    except OSError:
        pass


def _vault_readonly_conn() -> sqlite3.Connection | None:
    if not db.VAULT_DB.exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{db.VAULT_DB}?mode=ro",
            uri=True,
            timeout=1,
        )
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 100")
    except sqlite3.OperationalError:
        pass
    return conn


def _cold_lookup(file_path: str, limit: int) -> list[dict]:
    """Read-only lookup — does not touch the writer connection in db.get_db()."""
    conn = _vault_readonly_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            WITH RECURSIVE ancestors(summary_id, hop) AS (
                SELECT ss.summary_id, 0
                FROM summary_sources ss
                JOIN messages m
                  ON ss.source_type = 'message'
                 AND CAST(ss.source_id AS INTEGER) = m.id
                WHERE m.file_path = ?
                UNION
                SELECT ss.summary_id, a.hop + 1
                FROM ancestors a
                JOIN summary_sources ss
                  ON ss.source_type = 'summary'
                 AND ss.source_id = a.summary_id
                WHERE a.hop < 16
            )
            SELECT s.*
            FROM summaries s
            JOIN ancestors a ON a.summary_id = s.id
            WHERE COALESCE(s.consolidated, 0) = 0
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (file_path, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def get_file_fingerprint(file_path: str, limit: int = DEFAULT_LIMIT) -> str:
    """
    Return the formatted fingerprint for ``file_path`` or an empty string.

    Gated on ``fileContextEnabled`` (returns '' when disabled).
    Serves from cache when a fresh entry exists, otherwise runs a cold
    read-only lookup and writes the result back to cache.
    """
    if not file_path:
        return ""
    cfg = db.load_config()
    if not cfg.get("fileContextEnabled", False):
        return ""

    now = time.time()
    cache = _load_cache()
    entry = cache.get(file_path)
    if entry and (now - entry.get("ts", 0)) < CACHE_TTL_SECONDS:
        return entry.get("output", "")

    # Stampede guard: only one process runs the cold path per file at a time.
    claim = _claim_inflight(file_path)
    if claim is None:
        # Another process is already computing it — serve stale if present.
        return entry.get("output", "") if entry else ""

    try:
        summaries = _cold_lookup(file_path, limit)
        output = format_file_fingerprint(file_path, summaries)
        cache[file_path] = {"ts": now, "output": output}
        _store_cache(cache)
        return output
    finally:
        _release_inflight(claim, file_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    out = get_file_fingerprint(args.file, limit=args.limit)
    if out:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()
