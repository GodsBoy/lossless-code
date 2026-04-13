#!/bin/bash
# lossless-code: surface prior vault activity on a file before the tool runs.
# Default-off: gated on fileContextEnabled in the Python helper.
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null || echo "")
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ti = d.get('tool_input') or {}
print(ti.get('file_path') or ti.get('notebook_path') or ti.get('path') or '')
" 2>/dev/null || echo "")

case "$TOOL_NAME" in
    Read|Edit|Write|MultiEdit|NotebookEdit) ;;
    *) exit 0 ;;
esac

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

CONTEXT=$(python3 "$SCRIPTS_DIR/file_context.py" --file "$FILE_PATH" 2>/dev/null || echo "")

if [ -n "$CONTEXT" ]; then
    python3 -c "
import json, sys
ctx = sys.stdin.read()
if ctx.strip():
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'additionalContext': ctx
        }
    }))
" <<< "$CONTEXT"
fi

exit 0
