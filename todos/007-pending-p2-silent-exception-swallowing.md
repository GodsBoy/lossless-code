---
status: pending
priority: p2
issue_id: "007"
tags: [code-review, quality, python, error-handling]
dependencies: []
---

# Bare `except Exception: pass` Swallows Embedding Failures Silently

## Problem Statement

Multiple `except Exception: pass` blocks in `embed.py` silently discard failures with no log output. A systematic error (wrong schema, connection issue, type error in `vec_to_blob`) returns `stored = 0` with no signal to the operator. Users running `lcc reindex` or background embedding have no way to know why embeddings are not being stored.

## Findings

**Agents:** kieran-python-reviewer (P2), security-sentinel (P3)

- `embed.py:216` — upsert loop in `embed_messages_batch`
- `embed.py:270` — upsert loop in `reindex_vault`
- `embed.py:106` — `_fastembed_embed` inner exception
- `embed.py:126` — `_openai_embed` per-batch exception
- `embed.py:144` — `_anthropic_embed` per-batch exception

`reindex_vault` already prints progress to `sys.stderr` — apply the same discipline to error paths.

## Proposed Solutions

### Option A — Log to stderr with message context (Recommended)

```python
except Exception as exc:
    print(f"[lossless] embed store failed for message {row['id']}: {exc}", file=sys.stderr)
```

For API batch failures:
```python
except openai.AuthenticationError as exc:
    print(f"[lossless] OpenAI API key rejected: {exc}", file=sys.stderr)
    results.extend([None] * len(batch))
except Exception as exc:
    print(f"[lossless] embed batch failed: {exc}", file=sys.stderr)
    results.extend([None] * len(batch))
```

- Pros: Operator-visible failures; non-breaking (still returns None entries / skips row)
- Cons: None
- Effort: Small

## Recommended Action

_Option A — log to stderr with context. The project already uses `print(..., file=sys.stderr)` for warnings._

## Technical Details

**Affected files:** `scripts/embed.py`

## Acceptance Criteria

- [ ] No bare `except Exception: pass` in embed.py
- [ ] Failed upserts print `[lossless] embed store failed for message {id}: {error}` to stderr
- [ ] Failed API batches print `[lossless] embed batch failed: {error}` to stderr
- [ ] API auth failures produce a distinct, actionable message
- [ ] Embedding failures still non-fatal (function continues with remaining messages)

## Work Log

- 2026-03-29 — Identified by kieran-python-reviewer, security-sentinel
