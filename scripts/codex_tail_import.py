"""Project-scoped local session tail import for Codex continuity."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import db

AGENT_SOURCE = "codex-cli"
RESERVED_MARKER_PREFIX = "[lcc."

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"),
)


@dataclass
class SessionCandidate:
    path: Path
    session_id: str
    cwd: str
    timestamp: int
    mtime: float


def _is_unsafe_text(value: str) -> bool:
    return "\n" in value or "\r" in value or RESERVED_MARKER_PREFIX in value


def _clean_metadata_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or _is_unsafe_text(text):
        return ""
    return text


def normalize_path(value: str | Path | None) -> str:
    """Return a stable absolute path string for matching and storage."""
    if value is None:
        return ""
    text = _clean_metadata_text(str(value))
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return os.path.abspath(os.path.expanduser(text))


def _compare_path(value: str | Path | None) -> str:
    normalized = normalize_path(value)
    return os.path.normcase(normalized) if normalized else ""


def _path_is_within(parent: str | Path, child: str | Path) -> bool:
    parent_cmp = _compare_path(parent)
    child_cmp = _compare_path(child)
    if not parent_cmp or not child_cmp:
        return False
    try:
        return os.path.commonpath([parent_cmp, child_cmp]) == parent_cmp
    except ValueError:
        return False


def project_root_for_cwd(cwd: str | Path | None) -> str:
    cwd_path = normalize_path(cwd)
    if not cwd_path:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd_path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cwd_path
    if proc.returncode != 0:
        return cwd_path
    root = _clean_metadata_text(proc.stdout.strip())
    return normalize_path(root) or cwd_path


def _configured_project_roots(config: dict) -> list[str]:
    raw_roots = config.get("codexTailImportProjectRoots", [])
    if isinstance(raw_roots, str):
        raw_roots = [raw_roots]
    if not isinstance(raw_roots, list):
        return []
    roots = []
    for raw in raw_roots:
        normalized = normalize_path(raw)
        if normalized:
            roots.append(normalized)
    return roots


def is_project_opted_in(cwd: str | Path | None, config: dict | None = None) -> bool:
    config = config or db.load_config()
    project_root = project_root_for_cwd(cwd)
    if not project_root:
        return False
    return any(_compare_path(project_root) == _compare_path(root) for root in _configured_project_roots(config))


def set_project_opt_in(
    config: dict,
    cwd: str | Path | None,
    enabled: bool,
) -> tuple[dict, str]:
    """Return updated config plus the normalized project root."""
    updated = dict(config)
    project_root = project_root_for_cwd(cwd)
    if not project_root:
        raise ValueError("cwd is required")
    roots = _configured_project_roots(updated)
    root_cmp = _compare_path(project_root)
    kept = [root for root in roots if _compare_path(root) != root_cmp]
    if enabled:
        kept.append(project_root)
    updated["codexTailImportProjectRoots"] = kept
    return updated, project_root


def codex_sessions_dir(codex_home: str | Path | None = None) -> Path:
    return Path(codex_home or Path.home() / ".codex").expanduser() / "sessions"


def _parse_timestamp(value: object, fallback: float = 0) -> int:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number = number / 1000
        return int(number)
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                number = float(text)
                if number > 10_000_000_000:
                    number = number / 1000
                return int(number)
            except ValueError:
                pass
            try:
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                return int(datetime.fromisoformat(text).timestamp())
            except ValueError:
                pass
    return int(fallback or 0)


def _iter_jsonl_files(
    sessions_dir: Path,
    max_files: int,
    deadline: float | None = None,
) -> Iterable[Path]:
    candidates: list[tuple[float, Path]] = []
    try:
        paths = sessions_dir.rglob("*.jsonl")
        for path in paths:
            if deadline and time.monotonic() > deadline:
                break
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[:max(1, max_files)]]


def read_session_meta(path: Path, max_lines: int = 50) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for index, line in enumerate(f):
                if index >= max_lines:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else None
    except OSError:
        return None
    return None


def find_latest_matching_session(
    project_root: str | Path,
    *,
    codex_home: str | Path | None = None,
    exclude_session_id: str | None = None,
    max_files: int = 200,
    deadline: float | None = None,
) -> SessionCandidate | None:
    root = normalize_path(project_root)
    if not root:
        return None
    sessions_dir = codex_sessions_dir(codex_home)
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return None
    best: SessionCandidate | None = None
    for path in _iter_jsonl_files(sessions_dir, max_files, deadline=deadline):
        if deadline and time.monotonic() > deadline:
            break
        meta = read_session_meta(path)
        if not meta:
            continue
        session_id = _clean_metadata_text(meta.get("id"))
        if not session_id or session_id == exclude_session_id:
            continue
        meta_cwd = normalize_path(meta.get("cwd"))
        if not meta_cwd or not _path_is_within(root, meta_cwd):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        candidate = SessionCandidate(
            path=path,
            session_id=session_id,
            cwd=meta_cwd,
            timestamp=_parse_timestamp(meta.get("timestamp"), fallback=mtime),
            mtime=mtime,
        )
        if best is None or (candidate.timestamp, candidate.mtime) > (best.timestamp, best.mtime):
            best = candidate
    return best


def read_bounded_tail(path: Path, *, max_lines: int, max_bytes: int) -> tuple[list[str], bool]:
    """Read a bounded JSONL tail. Returns lines plus whether truncation happened."""
    max_lines = max(1, int(max_lines))
    max_bytes = max(1024, int(max_bytes))
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            truncated = size > max_bytes
            if truncated:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
    except OSError:
        return [], True
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, truncated


def _extract_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            text = item.get(key)
            if isinstance(text, str):
                parts.append(text)
                break
    return "\n".join(parts)


def parse_tail_messages(lines: list[str]) -> list[dict]:
    messages: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _extract_text(payload.get("content"))
        if text:
            messages.append({"role": role, "text": text})
    return messages


def _redact(value: str) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _compact_line(value: str, max_len: int = 220) -> str:
    text = _redact(value or "")
    if _is_unsafe_text(text):
        return ""
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _latest_message(messages: list[dict], role: str) -> str:
    for message in reversed(messages):
        if message.get("role") == role:
            return str(message.get("text") or "")
    return ""


def _extract_label(text: str, labels: tuple[str, ...]) -> str:
    boundary_labels = (
        "Goal",
        "Task",
        "Completed",
        "Last completed",
        "Done",
        "Next step",
        "Next",
        "Blocker",
        "Blocked",
    )
    boundary = "|".join(re.escape(label) for label in boundary_labels)
    for label in labels:
        match = re.search(
            rf"(?ims)\b{re.escape(label)}\s*:\s*(.+?)(?=\s+\b(?:{boundary})\s*:|$)",
            text,
        )
        if match:
            return match.group(1).strip()
    return ""


def _first_sentence(text: str) -> str:
    cleaned = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not cleaned:
        return ""
    match = re.match(r"(.+?[.!?])(?:\s|$)", cleaned)
    return match.group(1) if match else cleaned


def _find_blockers(messages: list[dict]) -> str:
    blocker_lines = []
    for message in messages[-6:]:
        text = str(message.get("text") or "")
        for line in text.splitlines():
            lowered = line.lower()
            if any(word in lowered for word in ("blocked", "blocker", "failed", "failure", "cannot", "error")):
                blocker_lines.append(line.strip())
    return "; ".join(blocker_lines[-2:])


def extract_task_state(
    messages: list[dict],
    *,
    candidate: SessionCandidate,
    tail_truncated: bool,
    tail_line_count: int,
) -> dict:
    user_text = _latest_message(messages, "user")
    assistant_text = _latest_message(messages, "assistant")
    goal = _extract_label(user_text, ("Goal", "Task")) or _first_sentence(user_text)
    last_step = (
        _extract_label(assistant_text, ("Completed", "Last completed", "Done"))
        or _first_sentence(assistant_text)
    )
    next_step = _extract_label(assistant_text, ("Next", "Next step"))
    blockers = _find_blockers(messages)

    goal = _compact_line(goal)
    last_step = _compact_line(last_step)
    next_step = _compact_line(next_step)
    blockers = _compact_line(blockers)

    warnings = []
    if tail_truncated:
        warnings.append("tail truncated")
    if not next_step:
        warnings.append("next step unavailable")
    if not goal and not last_step:
        warnings.append("no clear task state found")

    confidence = "medium" if goal and last_step and not warnings else "low"
    status = "found" if confidence == "medium" else "partial"
    when = (
        datetime.fromtimestamp(candidate.timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if candidate.timestamp
        else "unknown"
    )
    source_pointer = (
        f"codex-session:{candidate.session_id}; timestamp={when}; "
        f"tail_lines={tail_line_count}; truncated={str(tail_truncated).lower()}"
    )
    return {
        "source_runtime": AGENT_SOURCE,
        "source_session_id": candidate.session_id,
        "source_timestamp": candidate.timestamp,
        "source_pointer": source_pointer,
        "goal": goal,
        "last_step": last_step,
        "next_step": next_step,
        "blockers": blockers,
        "confidence": confidence,
        "status": status,
        "warning": "; ".join(warnings),
    }


def refresh_imported_task_state(
    *,
    working_dir: str | Path | None,
    current_session_id: str | None,
    config: dict | None = None,
    codex_home: str | Path | None = None,
) -> dict:
    """Refresh imported task state for an opted-in project.

    The return shape is intentionally small so hook callers can surface
    warnings without exposing local transcript text.
    """
    config = config or db.load_config()
    project_root = project_root_for_cwd(working_dir)
    if not project_root:
        return {"status": "skipped", "reason": "missing working directory"}
    if not is_project_opted_in(project_root, config):
        return {"status": "skipped", "project_root": project_root, "reason": "project not opted in"}

    timeout_ms = int(config.get("codexTailImportTimeoutMs", 1500) or 1500)
    deadline = time.monotonic() + max(timeout_ms, 100) / 1000
    codex_home = codex_home or config.get("codexTailImportCodexHome")
    candidate = find_latest_matching_session(
        project_root,
        codex_home=codex_home,
        exclude_session_id=current_session_id,
        max_files=int(config.get("codexTailImportMaxFiles", 200) or 200),
        deadline=deadline,
    )
    if candidate is None:
        return {"status": "unavailable", "project_root": project_root, "reason": "no matching session"}
    if time.monotonic() > deadline:
        return {"status": "unavailable", "project_root": project_root, "reason": "startup budget exceeded"}

    lines, tail_truncated = read_bounded_tail(
        candidate.path,
        max_lines=int(config.get("codexTailImportMaxTailLines", 120) or 120),
        max_bytes=int(config.get("codexTailImportMaxTailBytes", 120000) or 120000),
    )
    messages = parse_tail_messages(lines)
    if not messages:
        return {
            "status": "unavailable",
            "project_root": project_root,
            "source_session_id": candidate.session_id,
            "reason": "no message records in bounded tail",
        }
    task_state = extract_task_state(
        messages,
        candidate=candidate,
        tail_truncated=tail_truncated,
        tail_line_count=len(lines),
    )
    record_id = db.upsert_imported_task_state(project_root=project_root, **task_state)
    return {
        "status": task_state["status"],
        "project_root": project_root,
        "record_id": record_id,
        "source_session_id": candidate.session_id,
        "warning": task_state["warning"],
    }


def describe_project_import(
    *,
    cwd: str | Path | None,
    codex_home: str | Path | None = None,
    config: dict | None = None,
) -> dict:
    config = config or db.load_config()
    project_root = project_root_for_cwd(cwd)
    if not project_root:
        return {"status": "disabled", "detail": "missing working directory"}
    if not is_project_opted_in(project_root, config):
        return {
            "status": "disabled",
            "project_root": project_root,
            "detail": "disabled for current project",
        }
    sessions_dir = codex_sessions_dir(codex_home or config.get("codexTailImportCodexHome"))
    if not sessions_dir.exists():
        return {
            "status": "warn",
            "project_root": project_root,
            "detail": f"enabled for current project, but {sessions_dir} was not found",
        }
    latest = db.get_latest_imported_task_state(project_root, source_runtime=AGENT_SOURCE)
    if latest:
        imported_at = latest.get("imported_at") or 0
        freshness = datetime.fromtimestamp(imported_at).strftime("%Y-%m-%d %H:%M") if imported_at else "unknown"
        return {
            "status": "enabled",
            "project_root": project_root,
            "detail": (
                f"enabled for current project, latest status={latest.get('status')}, "
                f"confidence={latest.get('confidence')}, imported={freshness}"
            ),
        }
    return {
        "status": "enabled",
        "project_root": project_root,
        "detail": "enabled for current project, no imported task state yet",
    }


__all__ = [
    "SessionCandidate",
    "AGENT_SOURCE",
    "normalize_path",
    "project_root_for_cwd",
    "is_project_opted_in",
    "set_project_opt_in",
    "codex_sessions_dir",
    "read_session_meta",
    "find_latest_matching_session",
    "read_bounded_tail",
    "parse_tail_messages",
    "extract_task_state",
    "refresh_imported_task_state",
    "describe_project_import",
]
