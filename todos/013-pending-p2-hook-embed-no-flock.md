---
status: pending
priority: p2
issue_id: "013"
tags: [code-review, architecture, concurrency]
dependencies: []
---

# `hook_embed.py` Has No File Lock — Concurrent Stop Hooks Can Race

## Problem Statement

Phase 1 established that background processes writing shared SQLite state must use `fcntl.flock(LOCK_EX | LOCK_NB)` (documented in the Lossless Dream ADR). `hook_embed.py` writes to `message_embeddings` without a lock. If two Claude Code sessions end simultaneously, two `hook_embed.py` processes race on the same table. The UNIQUE constraint prevents data corruption but concurrent SQLite writers can produce `OperationalError: database is locked` which the current bare-except blocks swallow silently.

## Findings

**Agent:** learnings-researcher (P2, flagged as "Known Pattern")

- `scripts/hook_embed.py` — no `fcntl.flock` usage
- Phase 1 ADR (`docs/solutions/architecture-decisions/lossless-dream-pattern-extraction-and-dag-consolidation.md`) mandates: "Background processes writing shared state require `fcntl.flock(LOCK_EX | LOCK_NB)`"
- `scripts/hook_dream.py` (Phase 1) uses the flock pattern — reference implementation
- Concurrent writers: SQLite WAL mode reduces but does not eliminate locking conflicts for writers

## Proposed Solutions

### Option A — Add flock to `hook_embed.py` following `hook_dream.py` pattern (Recommended)

```python
import fcntl

LOCK_FILE = LOSSLESS_HOME / "embed.lock"

def main():
    cfg = db.load_config()
    if not cfg.get("embeddingEnabled", False):
        return
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # Another hook_embed.py is running, skip silently
    try:
        conn = db.get_db()
        embed.embed_messages_batch(conn, cfg, session_id=session_id)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
```

- Pros: Consistent with Phase 1 pattern; prevents SQLite lock errors; second process exits cleanly
- Cons: One session's new messages wait for the other to finish (acceptable — embedding is non-blocking to user)
- Effort: Small

## Recommended Action

_Option A — apply the flock pattern from hook_dream.py. The ADR mandates it for all background writers._

## Technical Details

**Affected files:** `scripts/hook_embed.py`

## Acceptance Criteria

- [ ] `hook_embed.py` acquires `LOCK_EX | LOCK_NB` on `~/.lossless-code/embed.lock` before writing
- [ ] Second concurrent instance exits cleanly (no error output)
- [ ] No SQLite `database is locked` errors in concurrent-session scenarios
- [ ] Pattern matches `hook_dream.py` implementation

## Work Log

- 2026-03-29 — Identified by learnings-researcher as known pattern from Phase 1 ADR

## Resources

- Phase 1 ADR: `docs/solutions/architecture-decisions/lossless-dream-pattern-extraction-and-dag-consolidation.md`
- Reference: `scripts/hook_dream.py` (flock pattern)
