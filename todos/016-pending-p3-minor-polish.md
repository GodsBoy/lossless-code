---
status: pending
priority: p3
issue_id: "016"
tags: [code-review, quality, python, polish]
dependencies: []
---

# Minor Polish: Type Annotations, Naming, Redundant Imports

## Problem Statement

Several small type annotation and naming issues across `embed.py` and `db.py`. None are critical but they degrade static analysis quality and readability.

## Findings

**Agent:** kieran-python-reviewer (P3), architecture-strategist (P3)

### Items

1. **`BATCH = 32` defined 3 times** — `embed.py:116`, `embed.py:135`, `embed.py:259`. Should be a single module-level constant `_EMBED_BATCH_SIZE = 32`. (Resolved if 015 is implemented — consolidates to one definition.)

2. **`import sys as _sys` inside `hybrid_search`** — `embed.py:317`. `sys` is already imported at module level (line 20). The alias `_sys` is unnecessary. Use `sys.stderr` directly.

3. **`Optional[str]` instead of `str | None`** — `embed.py:28, 77, 187, 228` and `db.py:673, 714`. The file has `from __future__ import annotations`, making `str | None` safe on Python 3.9+. Pick one style; new code should use `|` syntax.

4. **`detect_provider` return type should use `Literal`** — `embed.py:28`. Returns one of 4 known strings or None. `Literal["fastembed", "openai", "anthropic", "numpy"] | None` makes typos at call sites a static analysis error.

5. **`_vector_search_numpy` misleading name** — `embed.py:368`. The function runs for all embedding providers (fastembed, openai, anthropic), not just the numpy provider. It is the BLOB cosine search implementation. Better name: `_blob_cosine_search`.

6. **`ids: list` missing element type** — `db.py:333`. Should be `ids: list[int]`.

7. **`str(i)` coercion on integer IDs** — `db.py:338`. `messages.id` is `INTEGER PRIMARY KEY`. Passing `str(i)` relies on SQLite's implicit coercion. Drop the `str()` conversion.

8. **`working_dir: str = None` in pre-existing functions** — `db.py:521, 537, 641`. Touched-adjacent functions. Should be `working_dir: str | None = None`.

9. **reindex progress output goes to stdout** — `embed.py:237, 247, 257, 275, 283`. Progress/status output should go to `sys.stderr` (Unix convention). Structured output on stdout, progress on stderr.

## Acceptance Criteria

- [ ] `_EMBED_BATCH_SIZE = 32` module-level constant replaces 3 local definitions
- [ ] `import sys as _sys` removed from `hybrid_search`; `sys.stderr` used directly
- [ ] New code uses `str | None` not `Optional[str]`
- [ ] `detect_provider` return type annotated with `Literal[...]`
- [ ] `_vector_search_numpy` renamed to `_blob_cosine_search` (update all call sites)
- [ ] `ids: list[int]` in `get_messages_by_ids`
- [ ] `str(i)` removed from IDs list in `get_messages_by_ids`
- [ ] Progress prints in `reindex_vault` go to `sys.stderr`

## Work Log

- 2026-03-29 — Identified by kieran-python-reviewer, architecture-strategist
