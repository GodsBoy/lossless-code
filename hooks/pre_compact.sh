#!/bin/bash
# lossless-code: Persist messages to vault BEFORE Claude discards them
# Run summarisation in background so it doesn't block compaction
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Run DAG summarisation in background — don't block Claude's compaction
nohup python3 "$SCRIPTS_DIR/summarise.py" --session "$SESSION_ID" \
    >/dev/null 2>&1 &

exit 0
