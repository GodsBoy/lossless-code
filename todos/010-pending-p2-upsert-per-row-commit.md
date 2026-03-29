---
status: pending
priority: p2
issue_id: "010"
tags: [code-review, performance, database]
dependencies: ["001"]
---

# `upsert_embedding` Commits Once Per Row — Should Batch

## Problem Statement

`upsert_embedding()` calls `conn.commit()` after every single INSERT. Both `embed_messages_batch` and `reindex_vault` call it in a tight per-message loop. SQLite WAL commits are cheap but not free (~0.5–2ms each). For a 2,649-message vault this is ~1.3–5.3s of commit overhead during reindex. At 10,000 messages: ~5–20s wasted.

## Findings

**Agent:** performance-oracle (P2)

- `db.py:670` — `conn.commit()` inside `upsert_embedding` — fires per row
- `embed.py:209–217` — `embed_messages_batch` loop calls `upsert_embedding` per message
- `embed.py:266–274` — `reindex_vault` inner loop does the same
- Fix reduces 2,649 individual commits to ~83 batch commits (32× reduction)

## Proposed Solutions

### Option A — Remove `commit()` from `upsert_embedding`, commit at batch boundary (Recommended)

```python
# db.py
def upsert_embedding(conn: sqlite3.Connection, message_id: int, model_name: str, vector: bytes) -> None:
    now = int(time.time())
    conn.execute(
        """INSERT INTO message_embeddings (message_id, model_name, vector, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(message_id, model_name) DO UPDATE SET vector=excluded.vector, created_at=excluded.created_at""",
        (message_id, model_name, vector, now),
    )
    # Caller is responsible for commit

# embed.py — reindex_vault inner loop
for row, vec in zip(batch, vecs):
    if vec is None:
        continue
    try:
        blob = vec_to_blob(vec)
        _db.upsert_embedding(conn, row["id"], model, blob)
        stored += 1
    except Exception as exc:
        print(f"[lossless] embed store failed: {exc}", file=sys.stderr)
conn.commit()  # once per 32-message batch, not per row
```

- Pros: 32× fewer commits; SQLite handles WAL efficiently for batches
- Cons: Tests that call `upsert_embedding` directly may need explicit commit — check test suite
- Effort: Small
- Risk: Low — just moving where commit fires

## Recommended Action

_Option A — remove commit from `upsert_embedding`, commit at batch boundary in callers._

## Technical Details

**Affected files:** `scripts/db.py`, `scripts/embed.py`

## Acceptance Criteria

- [ ] `upsert_embedding` does NOT call `conn.commit()`
- [ ] `reindex_vault` calls `conn.commit()` once per batch (every 32 rows)
- [ ] `embed_messages_batch` calls `conn.commit()` once after the full loop
- [ ] Tests that call `upsert_embedding` directly call `conn.commit()` after
- [ ] 148 tests still pass

## Work Log

- 2026-03-29 — Identified by performance-oracle
