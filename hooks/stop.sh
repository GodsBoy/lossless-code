#!/bin/bash
# lossless-code: Persist each turn to vault.db on Stop event
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

python3 "$SCRIPTS_DIR/hook_stop.py" --session "$SESSION_ID" --dir "$CWD" 2>/dev/null || true

exit 0
