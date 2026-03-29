---
status: pending
priority: p1
issue_id: "001"
tags: [code-review, architecture, quality, python]
dependencies: []
---

# Dead `db_conn` Parameter in `embed_messages_batch`

## Problem Statement

`embed_messages_batch(db_conn, cfg, session_id)` accepts a connection parameter but silently discards it, acquiring its own connection via `_db.get_db()`. The docstring claims the parameter "avoids circular import issues" but the implementation contradicts this. The caller (`hook_embed.py:29`) opens a connection and passes it — that connection is never used.

This is a misleading API contract. If the codebase ever moves toward connection pooling or test-isolated connections, silent discard will cause writes to the wrong database with no error raised.

## Findings

**Agents:** kieran-python-reviewer (P1), architecture-strategist (P1), code-simplicity-reviewer (P1)

- `embed.py:187` — parameter declared as `db_conn`
- `embed.py:214` — `_db.upsert_embedding(_db.get_db(), ...)` — ignores `db_conn`, acquires fresh connection
- `hook_embed.py:27–29` — caller opens `conn = db.get_db()` then passes it in; the open call is wasted

## Proposed Solutions

### Option A — Remove the dead parameter (Recommended)
Remove `db_conn` from the signature. The function already manages its own connection internally. Update `hook_embed.py` to not open a connection at all.

```python
def embed_messages_batch(cfg: dict, session_id: str | None = None) -> int:
```

- Pros: Simpler API, honest contract, no unused imports at call site
- Cons: None — the parameter was never honoured
- Effort: Small
- Risk: Low — purely cosmetic change to callers

### Option B — Actually use the passed connection
Thread `db_conn` through to `upsert_embedding`:

```python
def embed_messages_batch(conn: sqlite3.Connection, cfg: dict, session_id: str | None = None) -> int:
    ...
    _db.upsert_embedding(conn, row["id"], model, blob)
```

- Pros: Enables test isolation with an in-memory connection
- Cons: Requires matching `reindex_vault` pattern; `load_config`/`save_config` still use module singleton
- Effort: Medium

## Recommended Action

_Option A — Remove the parameter. It was never used and the singleton connection is fine for this tool's single-process use case._

## Technical Details

**Affected files:** `scripts/embed.py`, `scripts/hook_embed.py`, `tests/test_embed.py`
**Database changes:** None

## Acceptance Criteria

- [ ] `embed_messages_batch` signature has no `db_conn` / `conn` parameter
- [ ] `hook_embed.py` does not open a connection before calling `embed_messages_batch`
- [ ] All test calls updated to match new signature
- [ ] 148 tests still pass

## Work Log

- 2026-03-29 — Identified by 3 independent review agents (Python, Architecture, Simplicity)

## Resources

- `scripts/embed.py:187–225`
- `scripts/hook_embed.py:27–29`
