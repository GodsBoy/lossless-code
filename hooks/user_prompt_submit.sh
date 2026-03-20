#!/bin/bash
# lossless-code: Surface relevant summaries before Claude sees the prompt
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

# Extract user prompt from the 'query' field (Claude Code UserPromptSubmit schema)
QUERY=$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
q = d.get('query', '')
print(q[:500] if q else '')
" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || [ -z "$QUERY" ]; then
    exit 0
fi

# Store user message
python3 "$SCRIPTS_DIR/hook_store_message.py" --session "$SESSION_ID" --role user --content "$QUERY" --dir "$CWD" 2>/dev/null || true

# Surface relevant context
CONTEXT=$(python3 "$SCRIPTS_DIR/inject_context.py" --session "$SESSION_ID" --dir "$CWD" --query "$QUERY" --limit 3 2>/dev/null || echo "")

if [ -n "$CONTEXT" ]; then
    python3 -c "
import json, sys
ctx = sys.stdin.read()
if ctx.strip():
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'UserPromptSubmit',
            'additionalContext': ctx
        }
    }))
" <<< "$CONTEXT"
fi

exit 0
