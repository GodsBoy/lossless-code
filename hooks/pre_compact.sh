#!/bin/bash
# lossless-code: Persist messages to vault BEFORE Claude discards them
#
# CRITICAL: All output is suppressed to prevent compaction loops.
# Any stdout from a hook can cause Claude Code to re-evaluate context,
# pushing tokens back up and triggering another compaction.
#
# A cooldown file prevents rapid re-triggering during compaction cascades.

# Suppress ALL stdout/stderr — the single most important line in this file
exec >/dev/null 2>&1

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
COOLDOWN_FILE="/tmp/.lossless-code-precompact-cooldown"
COOLDOWN_SECS=60

# Debounce: skip if last pre-compact ran within cooldown period
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

# Run DAG summarisation in background — don't block Claude's compaction
# </dev/null disconnects stdin, >/dev/null 2>&1 suppresses output, disown detaches
nohup python3 "$SCRIPTS_DIR/summarise.py" --session "$SESSION_ID" \
    </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true

exit 0
