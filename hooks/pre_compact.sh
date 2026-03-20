#!/bin/bash
# lossless-code: Run DAG summarisation before Claude compacts context
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Run full DAG summarisation pass
python3 "$SCRIPTS_DIR/summarise.py" --session "$SESSION_ID" 2>/dev/null || true

exit 0
