"""Dream cycle log entries for the lossless-code vault.

Named after the ``dream_log`` table it owns, matching the
``messages``/``sessions``/``summaries``/``embeddings`` naming pattern in this
package. Also avoids ambiguity with the top-level ``scripts/dream.py`` runner
when ``scripts/`` is on ``sys.path`` and a bare ``import dream`` is resolved.
"""

import hashlib
import os
import time
from typing import Optional


def project_hash(working_dir: str) -> str:
    """Deterministic hash for a working directory path (SHA-256, 16 hex chars)."""
    return hashlib.sha256(os.path.abspath(working_dir).encode()).hexdigest()[:16]


def get_last_dream(project_hash_val: str) -> Optional[dict]:
    """Get the most recent dream_log entry for a project."""
    from . import get_db
    db = get_db()
    row = db.execute(
        "SELECT * FROM dream_log WHERE project_hash = ? ORDER BY dreamed_at DESC LIMIT 1",
        (project_hash_val,),
    ).fetchone()
    return dict(row) if row else None


def store_dream_log(
    project_hash_val: str,
    scope: str,
    patterns_found: int,
    consolidations: int,
    sessions_analyzed: int,
    report_path: str = "",
    dreamed_at: Optional[int] = None,
    mode: Optional[str] = None,
) -> int:
    """Record a dream cycle in the log. Returns the new row id.

    ``mode`` (v1.2 U6) records whether the contract/decision extractors
    ran via LLM, fell back to regex, or failed. Read by lcc_status to
    surface degraded-mode operation to the user. NULL on legacy rows.
    """
    from . import get_db
    db = get_db()
    now = dreamed_at if dreamed_at is not None else int(time.time())
    cur = db.execute(
        """INSERT INTO dream_log
           (project_hash, scope, dreamed_at, patterns_found, consolidations,
            sessions_analyzed, report_path, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (project_hash_val, scope, now, patterns_found, consolidations,
         sessions_analyzed, report_path, mode),
    )
    db.commit()
    return cur.lastrowid


__all__ = ["project_hash", "get_last_dream", "store_dream_log"]
