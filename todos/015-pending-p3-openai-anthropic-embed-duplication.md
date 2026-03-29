---
status: pending
priority: p3
issue_id: "015"
tags: [code-review, quality, python, dry]
dependencies: []
---

# `_openai_embed` and `_anthropic_embed` Are Structurally Identical

## Problem Statement

Both functions implement the same batch loop with identical error handling, differing only in the import and client constructor. Any change to batch error handling, retry logic, or response parsing must be made twice. If Anthropic's embed API shape ever diverges from OpenAI's, the duplication makes it harder to fix, not easier.

## Findings

**Agent:** code-simplicity-reviewer (P1 for this item), kieran-python-reviewer

- `embed.py:110–127` — `_openai_embed`
- `embed.py:129–146` — `_anthropic_embed`
- Both: `BATCH = 32`, same loop, same `resp.data[i].embedding` shape, same `None` fill on exception

## Proposed Solution

Extract shared logic into `_api_embed(texts, model_name, client)`:

```python
def _api_embed(texts: list[str], model_name: str, client) -> list[Optional[list[float]]]:
    BATCH = 32
    results: list[Optional[list[float]]] = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        try:
            resp = client.embeddings.create(model=model_name, input=batch)
            for item in resp.data:
                results.append([float(v) for v in item.embedding])
        except Exception as exc:
            print(f"[lossless] embed batch failed: {exc}", file=sys.stderr)
            results.extend([None] * len(batch))
    return results

def _openai_embed(texts, model_name):
    try:
        import openai
        return _api_embed(texts, model_name, openai.OpenAI())
    except Exception:
        return [None] * len(texts)

def _anthropic_embed(texts, model_name):
    try:
        import anthropic
        return _api_embed(texts, model_name, anthropic.Anthropic())
    except Exception:
        return [None] * len(texts)
```

Saves ~18 lines, removes the duplication surface.

## Acceptance Criteria

- [ ] `_openai_embed` and `_anthropic_embed` delegate to shared `_api_embed`
- [ ] Both providers still work correctly
- [ ] Error logging from 007 applied to the shared function
- [ ] Tests still pass for both providers

## Work Log

- 2026-03-29 — Identified by code-simplicity-reviewer
