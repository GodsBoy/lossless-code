#!/bin/bash
set -euo pipefail

# lossless-code installer
# Idempotent — safe to run again to upgrade.

LOSSLESS_HOME="${LOSSLESS_HOME:-$HOME/.lossless-code}"
CLAUDE_DIR="$HOME/.claude"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing lossless-code..."
echo "  Home:    $LOSSLESS_HOME"
echo "  Source:  $SCRIPT_DIR"
echo ""

# ── 1. Create home directory ────────────────────────────────────────────

mkdir -p "$LOSSLESS_HOME/scripts"
echo "  [ok] Created $LOSSLESS_HOME"

# ── 2. Copy scripts ────────────────────────────────────────────────────

cp "$SCRIPT_DIR/scripts/db.py"                "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/summarise.py"         "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/inject_context.py"    "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/lcc.py"               "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_stop.py"         "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_session_start.py" "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_store_message.py" "$LOSSLESS_HOME/scripts/"
echo "  [ok] Copied Python scripts to $LOSSLESS_HOME/scripts/"

# ── 3. Copy and chmod CLI wrappers ──────────────────────────────────────

for cmd in lcc_grep lcc_expand lcc_context lcc_sessions lcc_handoff lcc_status; do
    cp "$SCRIPT_DIR/scripts/$cmd" "$LOSSLESS_HOME/scripts/$cmd"
    chmod +x "$LOSSLESS_HOME/scripts/$cmd"
done
echo "  [ok] Installed CLI commands"

# ── 4. Add to PATH via shell profile ────────────────────────────────────

LCC_PATH_LINE="export PATH=\"$LOSSLESS_HOME/scripts:\$PATH\""

for profile in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$profile" ]; then
        if ! grep -qF "lossless-code" "$profile" 2>/dev/null; then
            echo "" >> "$profile"
            echo "# lossless-code CLI tools" >> "$profile"
            echo "$LCC_PATH_LINE" >> "$profile"
            echo "  [ok] Added PATH to $profile"
        else
            echo "  [skip] PATH already in $profile"
        fi
    fi
done

# ── 4b. Create symlinks for non-login shell access ──────────────────────
# Hooks run in non-login shells where .bashrc/.zshrc may not be sourced.

mkdir -p "$HOME/.local/bin"
for cmd in lcc_grep lcc_expand lcc_context lcc_sessions lcc_handoff lcc_status; do
    ln -sf "$LOSSLESS_HOME/scripts/$cmd" /usr/local/bin/$cmd 2>/dev/null || \
    ln -sf "$LOSSLESS_HOME/scripts/$cmd" "$HOME/.local/bin/$cmd" 2>/dev/null || true
done
echo "  [ok] Created symlinks for PATH accessibility"

# Export for current session
export PATH="$LOSSLESS_HOME/scripts:$PATH"

# ── 5. Initialise vault.db ──────────────────────────────────────────────

python3 -c "
import sys
sys.path.insert(0, '$LOSSLESS_HOME/scripts')
import db
db.get_db()
db.close_db()
print('  [ok] Initialised vault.db')
"

# ── 6. Write default config.json ────────────────────────────────────────

if [ ! -f "$LOSSLESS_HOME/config.json" ]; then
    python3 -c "
import sys
sys.path.insert(0, '$LOSSLESS_HOME/scripts')
import db
db.save_config(db.DEFAULT_CONFIG)
print('  [ok] Created config.json with defaults')
"
else
    echo "  [skip] config.json already exists"
fi

# ── 7. Copy hooks ───────────────────────────────────────────────────────

mkdir -p "$LOSSLESS_HOME/hooks"
for hook in stop.sh session_start.sh user_prompt_submit.sh pre_compact.sh post_compact.sh; do
    cp "$SCRIPT_DIR/hooks/$hook" "$LOSSLESS_HOME/hooks/$hook"
    chmod +x "$LOSSLESS_HOME/hooks/$hook"
done
echo "  [ok] Installed hook scripts to $LOSSLESS_HOME/hooks/"

# ── 8. Configure Claude Code hooks in settings.json ─────────────────────

mkdir -p "$CLAUDE_DIR"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

if [ -f "$SETTINGS_FILE" ]; then
    # Merge hooks into existing settings using Python
    python3 << 'PYEOF'
import json
import os

settings_file = os.path.expanduser("~/.claude/settings.json")
lossless_home = os.environ.get("LOSSLESS_HOME", os.path.expanduser("~/.lossless-code"))

with open(settings_file) as f:
    settings = json.load(f)

hooks_dir = f"{lossless_home}/hooks"

lcc_hooks = {
    "SessionStart": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/session_start.sh",
            "timeout": 30
        }]
    }],
    "UserPromptSubmit": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/user_prompt_submit.sh",
            "timeout": 15
        }]
    }],
    "Stop": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/stop.sh",
            "timeout": 15
        }]
    }],
    "PreCompact": [{
        "matcher": "auto",
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/pre_compact.sh",
            "timeout": 120
        }]
    }, {
        "matcher": "manual",
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/pre_compact.sh",
            "timeout": 120
        }]
    }],
    "PostCompact": [{
        "matcher": "auto",
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/post_compact.sh",
            "timeout": 30
        }]
    }, {
        "matcher": "manual",
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/post_compact.sh",
            "timeout": 30
        }]
    }]
}

# Merge: keep existing hooks, add lcc ones
existing_hooks = settings.get("hooks", {})
for event, hook_list in lcc_hooks.items():
    if event not in existing_hooks:
        existing_hooks[event] = []
    # Remove any existing lossless-code hooks (for idempotency)
    existing_hooks[event] = [
        h for h in existing_hooks[event]
        if not any("lossless-code" in hh.get("command", "") for hh in h.get("hooks", []))
    ]
    existing_hooks[event].extend(hook_list)

settings["hooks"] = existing_hooks

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)

print("  [ok] Configured Claude Code hooks in settings.json")
PYEOF
else
    echo "  [warn] No settings.json found at $SETTINGS_FILE — create it manually or run 'claude' first"
fi

# ── 9. Install skill ────────────────────────────────────────────────────

SKILL_DIR="$CLAUDE_DIR/skills/lossless-code"
mkdir -p "$SKILL_DIR"
cp "$SCRIPT_DIR/skills/lossless-code/SKILL.md" "$SKILL_DIR/SKILL.md"
echo "  [ok] Installed skill to $SKILL_DIR"

# ── 10. Verify ──────────────────────────────────────────────────────────

echo ""
echo "Verifying installation..."

python3 -c "
import sys
sys.path.insert(0, '$LOSSLESS_HOME/scripts')
import db
conn = db.get_db()
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
expected = ['sessions', 'messages', 'summaries', 'summary_sources', 'messages_fts', 'summaries_fts']
missing = [t for t in expected if t not in tables]
if missing:
    print(f'  [FAIL] Missing tables: {missing}')
    sys.exit(1)
else:
    print('  [ok] All tables present in vault.db')
db.close_db()
"

# Quick status
export PATH="$LOSSLESS_HOME/scripts:$PATH"
python3 "$LOSSLESS_HOME/scripts/lcc.py" status

echo ""
echo "lossless-code installed successfully!"
echo ""
echo "Commands available: lcc_grep, lcc_expand, lcc_context, lcc_sessions, lcc_handoff, lcc_status"
echo "Hooks configured for: SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompact"
echo ""
echo "To uninstall, remove $LOSSLESS_HOME and the hooks from $SETTINGS_FILE"
