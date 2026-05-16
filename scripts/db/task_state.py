"""Imported task-state persistence helpers."""

from __future__ import annotations

import time

_TEXT_LIMITS = {
    "project_root": 512,
    "source_runtime": 80,
    "source_session_id": 160,
    "source_pointer": 240,
    "goal": 400,
    "last_step": 400,
    "next_step": 400,
    "blockers": 400,
    "confidence": 40,
    "status": 40,
    "warning": 300,
}


def _bounded_text(value: str | None, field: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if "[lcc." in text:
        return ""
    limit = _TEXT_LIMITS[field]
    if len(text) > limit:
        return text[: limit - 1] + "..."
    return text


def _row_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def upsert_imported_task_state(
    *,
    project_root: str,
    source_runtime: str,
    source_session_id: str,
    source_timestamp: int | None = None,
    source_pointer: str = "",
    goal: str = "",
    last_step: str = "",
    next_step: str = "",
    blockers: str = "",
    confidence: str = "low",
    status: str = "partial",
    warning: str = "",
    imported_at: int | None = None,
) -> int:
    """Insert or refresh a compact imported task-state record.

    This table deliberately stores only task-state fields plus source
    metadata. Raw turns remain outside this persistence path.
    """
    from . import get_db

    project_root = _bounded_text(project_root, "project_root")
    source_runtime = _bounded_text(source_runtime, "source_runtime")
    source_session_id = _bounded_text(source_session_id, "source_session_id")
    if not project_root or not source_runtime or not source_session_id:
        raise ValueError("project_root, source_runtime, and source_session_id are required")

    values = {
        "project_root": project_root,
        "source_runtime": source_runtime,
        "source_session_id": source_session_id,
        "source_timestamp": source_timestamp,
        "source_pointer": _bounded_text(source_pointer, "source_pointer"),
        "goal": _bounded_text(goal, "goal"),
        "last_step": _bounded_text(last_step, "last_step"),
        "next_step": _bounded_text(next_step, "next_step"),
        "blockers": _bounded_text(blockers, "blockers"),
        "confidence": _bounded_text(confidence, "confidence") or "low",
        "status": _bounded_text(status, "status") or "partial",
        "warning": _bounded_text(warning, "warning"),
        "imported_at": int(imported_at if imported_at is not None else time.time()),
    }
    conn = get_db()
    conn.execute(
        """INSERT INTO imported_task_state (
            project_root, source_runtime, source_session_id, source_timestamp,
            source_pointer, goal, last_step, next_step, blockers, confidence,
            status, warning, imported_at
        ) VALUES (
            :project_root, :source_runtime, :source_session_id, :source_timestamp,
            :source_pointer, :goal, :last_step, :next_step, :blockers, :confidence,
            :status, :warning, :imported_at
        )
        ON CONFLICT(project_root, source_runtime, source_session_id) DO UPDATE SET
            source_timestamp = excluded.source_timestamp,
            source_pointer = excluded.source_pointer,
            goal = excluded.goal,
            last_step = excluded.last_step,
            next_step = excluded.next_step,
            blockers = excluded.blockers,
            confidence = excluded.confidence,
            status = excluded.status,
            warning = excluded.warning,
            imported_at = excluded.imported_at""",
        values,
    )
    conn.commit()
    row = conn.execute(
        """SELECT id FROM imported_task_state
           WHERE project_root = ? AND source_runtime = ? AND source_session_id = ?""",
        (project_root, source_runtime, source_session_id),
    ).fetchone()
    return int(row["id"])


def get_latest_imported_task_state(
    project_root: str,
    source_runtime: str | None = None,
) -> dict | None:
    """Return the newest imported task-state record for a project."""
    from . import get_db

    project_root = _bounded_text(project_root, "project_root")
    if not project_root:
        return None
    conn = get_db()
    if source_runtime:
        row = conn.execute(
            """SELECT * FROM imported_task_state
               WHERE project_root = ? AND source_runtime = ?
               ORDER BY COALESCE(source_timestamp, 0) DESC, imported_at DESC, id DESC
               LIMIT 1""",
            (project_root, source_runtime),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM imported_task_state
               WHERE project_root = ?
               ORDER BY COALESCE(source_timestamp, 0) DESC, imported_at DESC, id DESC
               LIMIT 1""",
            (project_root,),
        ).fetchone()
    return _row_dict(row)


__all__ = ["upsert_imported_task_state", "get_latest_imported_task_state"]
