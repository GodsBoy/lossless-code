#!/bin/bash
# lossless-code: Persist conversation to vault.db on Stop event
# Reads stdin JSON from Claude Code, extracts transcript_path, and bulk-ingests messages.
set -euo pipefail

SCRIPTS_DIR="${LOSSLESS_HOME:-$HOME/.lossless-code}/scripts"
INPUT=$(cat)

# Parse all fields from stdin JSON in one pass
eval "$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'SESSION_ID={json.dumps(d.get(\"session_id\", \"\"))}')
print(f'CWD={json.dumps(d.get(\"cwd\", \"\"))}')
print(f'TRANSCRIPT_PATH={json.dumps(d.get(\"transcript_path\", \"\"))}')
print(f'STOP_HOOK_ACTIVE={json.dumps(str(d.get(\"stop_hook_active\", False)).lower())}')
" 2>/dev/null || echo 'SESSION_ID=""; CWD=""; TRANSCRIPT_PATH=""; STOP_HOOK_ACTIVE="false"')"

# Prevent infinite loops
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    exit 0
fi

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

python3 "$SCRIPTS_DIR/hook_stop.py" \
    --session "$SESSION_ID" \
    --dir "$CWD" \
    --transcript "$TRANSCRIPT_PATH" \
    2>/dev/null || true

# --- Dream auto-trigger (background, non-blocking) ---
# Skip dream for stateless sessions (e.g. subagents, cron jobs)
# Pass SESSION_ID via env var to avoid shell injection from special characters in IDs
SESSION_STATELESS=$(LOSSLESS_SESSION_ID="$SESSION_ID" LOSSLESS_SCRIPTS_DIR="$SCRIPTS_DIR" python3 -c "
import sys, os
sys.path.insert(0, os.environ['LOSSLESS_SCRIPTS_DIR'])
import db
sid = os.environ.get('LOSSLESS_SESSION_ID', '')
print('true' if sid and db.get_session_stateless(sid) else 'false')
" 2>/dev/null || echo "false")

if [ "$SESSION_STATELESS" != "true" ]; then
    SHOULD_DREAM=$(python3 "$SCRIPTS_DIR/dream.py" --check-trigger --cwd "$CWD" 2>/dev/null || echo "false")
    if [ "$SHOULD_DREAM" = "true" ]; then
        nohup python3 "$SCRIPTS_DIR/dream.py" --run --project "$CWD" \
            </dev/null >/dev/null 2>&1 &
        disown 2>/dev/null || true
    fi
fi

# --- Embedding auto-index (background, non-blocking) ---
EMBED_ENABLED=$(python3 -c "
import sys, os
sys.path.insert(0, '$SCRIPTS_DIR')
import db
cfg = db.load_config()
print('true' if cfg.get('embeddingEnabled', False) else 'false')
" 2>/dev/null || echo "false")
if [ "$EMBED_ENABLED" = "true" ]; then
    nohup python3 "$SCRIPTS_DIR/hook_embed.py" \
        --session "$SESSION_ID" --dir "$CWD" \
        </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi

exit 0
