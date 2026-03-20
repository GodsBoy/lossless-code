#!/bin/bash
# lossless-code: Record compaction event and link to DAG after compact
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Record the compaction event as a system message
python3 "$SCRIPTS_DIR/hook_store_message.py" \
    --session "$SESSION_ID" \
    --role tool \
    --tool-name "compaction" \
    --content "[Context compaction occurred at $(date -u +%Y-%m-%dT%H:%M:%SZ)]" \
    2>/dev/null || true

# Inject top summaries back so Claude retains critical context post-compaction
CONTEXT=$(python3 "$SCRIPTS_DIR/inject_context.py" --session "$SESSION_ID" --limit 5 2>/dev/null || echo "")

if [ -n "$CONTEXT" ]; then
    python3 -c "
import json, sys
ctx = sys.stdin.read()
if ctx.strip():
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PostCompact',
            'additionalContext': ctx
        }
    }))
" <<< "$CONTEXT"
fi

exit 0
