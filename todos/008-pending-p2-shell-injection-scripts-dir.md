---
status: pending
priority: p2
issue_id: "008"
tags: [code-review, security]
dependencies: []
---

# Shell Injection via `$SCRIPTS_DIR` String Interpolation in `stop.sh`

## Problem Statement

`stop.sh` embeds Python source code inline using shell variable interpolation. `$SCRIPTS_DIR` is expanded by the shell before Python runs. If `LOSSLESS_HOME` is influenced by an attacker (via `.env` auto-loading, compromised parent process, or a hook that sets environment variables), arbitrary Python can be injected into the `-c` string.

```bash
EMBED_ENABLED=$(python3 -c "
import sys, os
sys.path.insert(0, '$SCRIPTS_DIR')   # $SCRIPTS_DIR expanded by shell here
...
" 2>/dev/null || echo "false")
```

Additionally, `hook_embed.py` already gates on `embeddingEnabled` at lines 24–25 and exits early — making the shell-side check redundant.

## Findings

**Agents:** security-sentinel (P2), kieran-python-reviewer (P3), code-simplicity-reviewer (P3)

- `hooks/stop.sh:43–49` — inline Python with `$SCRIPTS_DIR` interpolation
- `hook_embed.py:24–25` — already gates: `if not cfg.get("embeddingEnabled", False): return`
- Shell check is redundant — `hook_embed.py` will simply exit immediately when disabled

## Proposed Solutions

### Option A — Remove the shell check, unconditionally spawn `hook_embed.py` (Recommended)

```bash
# Remove lines 43–55 (the EMBED_ENABLED check and conditional block)
# Replace with:
nohup python3 "$SCRIPTS_DIR/hook_embed.py" \
    --session "$SESSION_ID" --dir "$CWD" \
    </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true
```

Cost of always launching: one Python startup + immediate exit (~50ms) when disabled. Negligible.

- Pros: Eliminates injection vector entirely; simpler code; DRY (config read in one place)
- Cons: Spawns process even when embeddings disabled (trivial cost)
- Effort: Small (remove ~10 lines, simplify to 4 lines)
- Risk: Very low

### Option B — Use a Python script file instead of `-c` inline

```bash
EMBED_ENABLED=$(SCRIPTS_DIR="$SCRIPTS_DIR" python3 "$SCRIPTS_DIR/check_embed.py" 2>/dev/null || echo "false")
```

- Pros: No interpolation into source code; path passed as env var
- Cons: Requires a new `check_embed.py` file; more files than necessary
- Effort: Small but adds a file

## Recommended Action

_Option A — remove the redundant shell check. hook_embed.py's own early exit is the right gate._

## Technical Details

**Affected files:** `hooks/stop.sh`
**Lines to remove:** ~12 (lines 43–55 approximately)

## Acceptance Criteria

- [ ] No inline Python source code with shell variable interpolation in `stop.sh`
- [ ] `hook_embed.py` still runs and exits early when `embeddingEnabled=false`
- [ ] Verified that hook produces zero stdout (hook behavior contract)

## Work Log

- 2026-03-29 — Identified by security-sentinel (P2), confirmed redundant by simplicity reviewer
