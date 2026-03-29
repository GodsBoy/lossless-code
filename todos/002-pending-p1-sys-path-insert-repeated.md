---
status: pending
priority: p1
issue_id: "002"
tags: [code-review, architecture, quality, python]
dependencies: []
---

# `sys.path.insert()` Copy-Pasted in 4 Function Bodies

## Problem Statement

The same 2-line path setup is repeated inside `embed_messages_batch`, `reindex_vault`, `hybrid_search`, and `_vector_search_numpy`. Mutating `sys.path` inside function bodies is a structural smell — it fires on every call, silently accumulates duplicate entries in `sys.path`, and makes the module difficult to test in isolation. The deferred `import db as _db` pattern is legitimate for circular-import avoidance, but the path manipulation belongs at module level.

## Findings

**Agents:** kieran-python-reviewer (P1), architecture-strategist (P1), code-simplicity-reviewer (P2)

- `embed.py:193` — `sys.path.insert(0, ...)` + `import db as _db` inside `embed_messages_batch`
- `embed.py:233` — repeated inside `reindex_vault`
- `embed.py:302` — repeated inside `hybrid_search`
- `embed.py:375` — repeated inside `_vector_search_numpy`

`hook_embed.py:12` already does `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` at module level — the correct pattern. `embed.py` should follow the same approach.

## Proposed Solutions

### Option A — Module-level insert + top-level import (Recommended)

```python
# At the top of embed.py, after stdlib imports
import os as _os
_SCRIPTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import db as _db  # module-level, after path is set
```

Then remove the 4 repeated blocks inside function bodies.

- Pros: DRY, idiomatic, no repeated mutation, works correctly when called from nohup background
- Cons: Imports db at module load time — but this is fine since embed.py is always co-located with db.py and hook_embed.py already does this
- Effort: Small (add 3 lines at top, remove 8 lines from function bodies)

### Option B — Keep deferred imports, hoist path setup

Keep `import db as _db` deferred inside functions (for future flexibility), but do the `sys.path.insert` once at module level with an idempotency guard:

```python
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
```

- Pros: Preserves deferred import pattern; path setup is still a single, clear statement
- Cons: Functions still need their own `import db as _db` — slightly verbose
- Effort: Small

## Recommended Action

_Option A — move path setup and db import both to module level. `hook_embed.py` is the entry-point script that already guarantees the path is set up, so there is no circular-import risk in embed.py._

## Technical Details

**Affected files:** `scripts/embed.py`
**Lines removed:** ~8 (2 lines × 4 occurrences inside function bodies)

## Acceptance Criteria

- [ ] `sys.path.insert` appears exactly once in `embed.py` (at module level)
- [ ] `import db as _db` (or equivalent) not repeated inside function bodies
- [ ] No duplicate entries in `sys.path` when functions are called multiple times
- [ ] 148 tests still pass

## Work Log

- 2026-03-29 — Identified by 3 independent review agents

## Resources

- `scripts/embed.py:193, 233, 302, 375`
- `scripts/hook_embed.py:12` (reference pattern)
