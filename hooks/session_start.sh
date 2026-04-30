#!/bin/bash
# lossless-code: Inject handoff + relevant summaries on SessionStart
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

# Plugin installs do not run install.sh, so create user PATH shims from the
# plugin cache before skills try bare lcc_* Bash commands.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$SCRIPTS_DIR/ensure_cli_shims.py" ]; then
    python3 "$SCRIPTS_DIR/ensure_cli_shims.py" --quiet 2>/dev/null || true
fi

SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

# Ensure session exists in vault
python3 "$SCRIPTS_DIR/hook_session_start.py" --session "$SESSION_ID" --dir "$CWD" 2>/dev/null || true

# Build context and output as additionalContext
CONTEXT=$(python3 "$SCRIPTS_DIR/inject_context.py" --session "$SESSION_ID" --dir "$CWD" 2>/dev/null || echo "")

if [ -n "$CONTEXT" ]; then
    python3 -c "
import json, sys
ctx = sys.stdin.read()
if ctx.strip():
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': ctx
        }
    }))
" <<< "$CONTEXT"
fi

exit 0
