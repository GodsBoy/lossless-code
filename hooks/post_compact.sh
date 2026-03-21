#!/bin/bash
# lossless-code: Record compaction event (NO context injection — causes compact loops)
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

# DO NOT inject context back — it pushes tokens up and triggers
# another auto-compact, creating an infinite loop.
# Context recall is available via MCP tools (lcc_grep, lcc_expand).

exit 0
