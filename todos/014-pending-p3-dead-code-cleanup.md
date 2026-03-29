---
status: pending
priority: p3
issue_id: "014"
tags: [code-review, quality, python, cleanup]
dependencies: []
---

# Dead Code: blob_to_vec, vectorBackend Config, model_name Unused Param, Path Import

## Problem Statement

Four pieces of dead code across `embed.py` and `db.py`. Each misleads readers into thinking there is functionality that does not exist.

## Findings

**Agent:** code-simplicity-reviewer (P1/P2 for these items)

### Dead items

1. **`blob_to_vec()` function** — `embed.py:171–180` — never called in production code. `_vector_search_numpy` decodes BLOBs directly with `np.frombuffer` inline. Only used in one test (`test_round_trip`). The test is really validating `vec_to_blob` normalisation — `blob_to_vec` is test scaffolding exposed as a public function.

2. **`vectorBackend: "auto"` in DEFAULT_CONFIG** — `db.py:223` — key defined, never read by any business logic. Users or agents setting `"vectorBackend": "sqlite-vec"` get no effect and no error.

3. **`model_name` parameter in `get_all_messages_for_reindex`** — `db.py:695` — accepted but the query is `SELECT id, content FROM messages ORDER BY timestamp` with no reference to it. Dead parameter.

4. **`from pathlib import Path`** — `embed.py:21` — imported, never used. All file I/O delegated to `db.py`.

## Proposed Solutions

Remove all four:
- Delete `blob_to_vec` function; update `test_round_trip` to use `np.frombuffer` directly
- Remove `"vectorBackend": "auto"` from `DEFAULT_CONFIG`; remove from `_cfg()` in `tests/test_embed.py`
- Remove `model_name: str` param from `get_all_messages_for_reindex`; update call site in `reindex_vault`
- Remove `from pathlib import Path` import line

When sqlite-vec backend is implemented, re-add `vectorBackend` with working routing logic.

## Acceptance Criteria

- [ ] `blob_to_vec` removed from `embed.py`
- [ ] `vectorBackend` removed from `DEFAULT_CONFIG` and test fixture
- [ ] `get_all_messages_for_reindex` takes no parameters
- [ ] `from pathlib import Path` removed from `embed.py`
- [ ] No test regressions

## Work Log

- 2026-03-29 — Identified by code-simplicity-reviewer, architecture-strategist
