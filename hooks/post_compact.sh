#!/bin/bash
# lossless-code: Record compaction event (NO context injection)
#
# CRITICAL: All output is suppressed to prevent compaction loops.
# Context injection was removed — it pushed tokens up and re-triggered
# compaction in an infinite loop. Context recall is available via MCP
# tools (lcc_grep, lcc_expand, lcc_context).
#
# A cooldown file prevents rapid re-triggering during compaction cascades.

# Suppress ALL stdout/stderr — the single most important line in this file
exec >/dev/null 2>&1

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
COOLDOWN_FILE="/tmp/.lossless-code-postcompact-cooldown"
COOLDOWN_SECS=60

# Debounce: skip if last post-compact ran within cooldown period
if [ -f "$COOLDOWN_FILE" ]; then
    LAST=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST))
    if [ "$DIFF" -lt "$COOLDOWN_SECS" ] 2>/dev/null; then
        exit 0
    fi
fi

# Read stdin JSON from Claude Code
INPUT=$(cat || echo '{}')

SESSION_ID=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Mark cooldown BEFORE doing work — prevents re-entry
date +%s > "$COOLDOWN_FILE" 2>/dev/null || true

# Record the compaction event as a system message — silently
python3 "$SCRIPTS_DIR/hook_store_message.py" \
    --session "$SESSION_ID" \
    --role tool \
    --tool-name "compaction" \
    --content "[Context compaction occurred at $(date -u +%Y-%m-%dT%H:%M:%SZ)]" \
    >/dev/null 2>&1 || true

# DO NOT inject context back — it pushes tokens up and triggers
# another auto-compact, creating an infinite loop.
# Context recall is available via MCP tools (lcc_grep, lcc_expand).

exit 0
