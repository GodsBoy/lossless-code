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
mkdir -p "$LOSSLESS_HOME/mcp"
mkdir -p "$LOSSLESS_HOME/dream/reports" "$LOSSLESS_HOME/dream/global" "$LOSSLESS_HOME/dream/projects"
echo "  [ok] Created $LOSSLESS_HOME"

# ── 2. Copy scripts ────────────────────────────────────────────────────

# db is a package (scripts/db/) — remove any legacy db.py from older installs
# then sync the package directory fresh so removed submodules don't linger.
rm -f "$LOSSLESS_HOME/scripts/db.py"
rm -rf "$LOSSLESS_HOME/scripts/db"
cp -r "$SCRIPT_DIR/scripts/db"                "$LOSSLESS_HOME/scripts/db"
rm -rf "$LOSSLESS_HOME/scripts/db/__pycache__"

cp "$SCRIPT_DIR/scripts/summarise.py"         "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/inject_context.py"    "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/file_context.py"      "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/lcc.py"               "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/lcc"                  "$LOSSLESS_HOME/scripts/"
chmod +x "$LOSSLESS_HOME/scripts/lcc"
cp "$SCRIPT_DIR/scripts/codex_support.py"     "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/codex_session_start.py" "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_stop.py"          "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_session_start.py" "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_store_message.py" "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_store_tool_call.py" "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/dream.py"             "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/embed.py"             "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/hook_embed.py"        "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/check_summariser_pollution.py" "$LOSSLESS_HOME/scripts/"
# v1.2 additions: contracts extractor + shared status helper
cp "$SCRIPT_DIR/scripts/contracts.py"         "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/lcc_core.py"          "$LOSSLESS_HOME/scripts/"
cp "$SCRIPT_DIR/scripts/ensure_cli_shims.py" "$LOSSLESS_HOME/scripts/"
echo "  [ok] Copied Python scripts to $LOSSLESS_HOME/scripts/"

# ── 2b. Copy MCP server ──────────────────────────────────────────────────

cp "$SCRIPT_DIR/mcp/server.py" "$LOSSLESS_HOME/mcp/server.py"
echo "  [ok] Copied MCP server to $LOSSLESS_HOME/mcp/"

# ── 3. Copy and chmod CLI wrappers ──────────────────────────────────────

for cmd in lcc_grep lcc_expand lcc_context lcc_sessions lcc_handoff lcc_status lcc_dream; do
    cp "$SCRIPT_DIR/scripts/$cmd" "$LOSSLESS_HOME/scripts/$cmd"
    chmod +x "$LOSSLESS_HOME/scripts/$cmd"
done
echo "  [ok] Installed CLI commands"

# Install lcc CLI
cp "$SCRIPT_DIR/scripts/lcc" "$LOSSLESS_HOME/scripts/lcc"
chmod +x "$LOSSLESS_HOME/scripts/lcc"
echo "  [ok] Installed lcc CLI"

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
for cmd in lcc_grep lcc_expand lcc_context lcc_sessions lcc_handoff lcc_status lcc_dream lcc; do
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
for hook in stop.sh session_start.sh user_prompt_submit.sh pre_compact.sh post_compact.sh pre_tool_use.sh post_tool_use.sh; do
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
    # PreCompact and PostCompact are registered here for standalone installs.
    # If the lossless-code plugin is also active, the cooldown mechanism in
    # each hook script prevents double execution.
    "PreCompact": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/pre_compact.sh",
            "timeout": 10
        }]
    }],
    "PostCompact": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/post_compact.sh",
            "timeout": 10
        }]
    }],
    # PreToolUse/PostToolUse power the fingerprint file-context feature.
    # Both are gated on fileContextEnabled (default false) in config.json,
    # so registering them here is a no-op until the flag flips.
    "PreToolUse": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/pre_tool_use.sh",
            "timeout": 5
        }]
    }],
    "PostToolUse": [{
        "hooks": [{
            "type": "command",
            "command": f"{hooks_dir}/post_tool_use.sh",
            "timeout": 5
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

# ── 8b. Auto-approve MCP tool permissions ────────────────────────────
# Without this, Claude Code prompts for permission on every MCP tool call.
permissions = settings.get("permissions", {})
allow = permissions.get("allow", [])
# Single wildcard rule covers all lcc tools
mcp_rule = "mcp__lossless-code__*"
# Remove any old per-tool rules (from earlier installs)
allow = [a for a in allow if not a.startswith("mcp__lossless-code__")]
if mcp_rule not in allow:
    allow.append(mcp_rule)
permissions["allow"] = allow
settings["permissions"] = permissions

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)

print("  [ok] Configured Claude Code hooks in settings.json")
print("  [ok] Auto-approved MCP tool permissions")
PYEOF
else
    echo "  [warn] No settings.json found at $SETTINGS_FILE — create it manually or run 'claude' first"
fi

# ── 9. Install Python dependencies ────────────────────────────────────

if python3 -c "import mcp" 2>/dev/null; then
    echo "  [skip] MCP SDK already installed"
else
    echo "  [info] Installing MCP SDK..."
    pip install --break-system-packages mcp 2>/dev/null || \
    pip install mcp 2>/dev/null || \
    echo "  [warn] Could not install mcp — install manually: pip install mcp"
fi

if python3 -c "import textual" 2>/dev/null; then
    echo "  [skip] textual already installed"
else
    echo "  [info] Installing textual (TUI framework)..."
    pip install --break-system-packages textual 2>/dev/null || \
    pip install textual 2>/dev/null || \
    echo "  [warn] Could not install textual — install manually: pip install textual"
fi

# ── 10. Register MCP server in ~/.claude.json ──────────────────────────
#
# Precedence model: when the lossless-code Claude Code plugin is installed,
# the plugin registers its own project-scope MCP server via .mcp.json. A
# user-scope registration from this script would create a conflicting
# endpoint for the same server name (different commands, different OAuth
# token scopes), which `claude /doctor` surfaces as a warning.
#
# Strategy: detect the plugin via $CLAUDE_DIR/plugins/installed_plugins.json
# and skip (or strip) the user-scope registration when the plugin is
# present. Manual-install-only users (no plugin) continue to get the
# user-scope registration so lossless-code works after one command.
#
# This section diverges from section 8's strip-then-always-write hook
# idempotency pattern: hooks are event-dispatch targets that can safely
# coexist (with cooldown logic in each hook script), but MCP registration
# is a name-based endpoint map where two registrations conflict silently
# at runtime. Avoidance (skip when plugin is present) is the only safe
# strategy for MCP, hence the explicit branching here.

CLAUDE_DIR="$CLAUDE_DIR" LOSSLESS_HOME="$LOSSLESS_HOME" python3 << 'MCPEOF'
import json
import os
import sys
import tempfile

claude_json = os.path.expanduser("~/.claude.json")
claude_dir = os.environ.get("CLAUDE_DIR") or os.path.expanduser("~/.claude")
installed_plugins_json = os.path.join(claude_dir, "plugins", "installed_plugins.json")
lossless_home = os.environ.get("LOSSLESS_HOME") or os.path.expanduser("~/.lossless-code")
mcp_server_path = os.path.join(lossless_home, "mcp", "server.py")

# Detect plugin presence. Any failure degrades to "plugin not installed"
# so the manual-install fallback runs. Never raise — a partial plugin
# registry should not break a manual install. Broad `Exception` catch
# covers OSError, JSONDecodeError, UnicodeDecodeError, AttributeError on
# non-dict values, and any other surprises from a malformed registry.
plugin_installed = False
plugins_map = {}
try:
    with open(installed_plugins_json) as f:
        registry = json.load(f)
    plugins_map = registry.get("plugins") or {}
    if not isinstance(plugins_map, dict):
        plugins_map = {}
    entries = plugins_map.get("lossless-code@lossless-code") or []
    plugin_installed = bool(entries)
except Exception:
    plugin_installed = False
    plugins_map = {}

# Diagnostic: if the plugin registry is readable and non-empty but our
# expected key is absent while a lossless-code-prefixed key is present,
# warn. This catches silent false-negatives from a marketplace key-format
# change (otherwise the script would silently register a duplicate
# endpoint alongside the plugin's own registration).
if not plugin_installed and plugins_map:
    suspicious = [k for k in plugins_map if isinstance(k, str) and k.startswith("lossless-code")]
    if suspicious:
        print(f"  [warn] Plugin registry has lossless-code entries but not 'lossless-code@lossless-code': {suspicious}")
        print(f"  [warn] Falling back to manual install. Check the marketplace metadata if this is unexpected.")

# Load existing ~/.claude.json. Guard against missing, unreadable, or
# malformed content — the file is user-facing state that may have been
# truncated by a prior crash or hand-edited. Do not silently default to
# an empty config on parse failure; that would discard the user's other
# mcpServers entries.
config = {}
if os.path.exists(claude_json):
    try:
        with open(claude_json) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"  [error] {claude_json} is unreadable or contains invalid JSON: {exc}")
        print(f"  [error] Refusing to overwrite. Inspect the file manually and re-run install.sh.")
        sys.exit(1)

mcp_servers = config.get("mcpServers") or {}
if not isinstance(mcp_servers, dict):
    mcp_servers = {}

dirty = False

if plugin_installed:
    # Plugin is authoritative. Strip any stale user-scope registration so
    # the plugin endpoint is the only one Claude Code sees.
    if "lossless-code" in mcp_servers:
        del mcp_servers["lossless-code"]
        dirty = True
        print(f"  [ok] Removed user-scope MCP registration (plugin provides it)")
    else:
        print(f"  [ok] Plugin detected. Skipping manual MCP registration (plugin provides it).")
    # v1.2 multi-install drift warning. install.sh is being invoked despite
    # the plugin already being present. The vault, hooks, and MCP server
    # all coexist at the same paths, so both copies of every script are
    # live. A bug fix that ships in main does not reach the plugin cache
    # until the next plugin update; conversely, a fix that ships in the
    # plugin does not reach the manual install until the user re-runs
    # install.sh. Warn now so the user can decide which install to keep.
    print("", file=sys.stderr)
    print(
        "  [warn] Both manual install (this script) and plugin install detected.",
        file=sys.stderr,
    )
    print(
        "  [warn] Coexistence works but creates drift on every release.",
        file=sys.stderr,
    )
    print(
        "  [warn] To run plugin only: rm -rf ~/.lossless-code/ after this install.",
        file=sys.stderr,
    )
    print(
        "  [warn] To run manual only: claude plugin remove lossless-code",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
else:
    # No plugin. Register the manual-install MCP (preserve other servers).
    desired = {
        "command": "python3",
        "args": [mcp_server_path],
        "env": {}
    }
    if mcp_servers.get("lossless-code") != desired:
        mcp_servers["lossless-code"] = desired
        dirty = True
    print(f"  [ok] Registered MCP server in {claude_json} (manual install)")

# Only write when something actually changed. Avoids reformatting the
# file and touching its mtime when the script is a no-op (plugin
# installed + no stale entry, or manual re-run with identical config).
if dirty:
    config["mcpServers"] = mcp_servers
    # Atomic write: write to a temp file in the same directory, then
    # os.replace() into place. If the process is killed mid-write, the
    # original file is left intact instead of being truncated to zero.
    parent_dir = os.path.dirname(claude_json) or "."
    with tempfile.NamedTemporaryFile(
        mode="w", dir=parent_dir, prefix=".claude.json.",
        suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(config, tmp, indent=2)
        tmp.write("\n")
        tmp_path = tmp.name
    os.replace(tmp_path, claude_json)
MCPEOF

# ── 11. Install skill ────────────────────────────────────────────────────

SKILL_DIR="$CLAUDE_DIR/skills/lossless-code"
mkdir -p "$SKILL_DIR"
cp "$SCRIPT_DIR/skills/lossless-code/SKILL.md" "$SKILL_DIR/SKILL.md"
echo "  [ok] Installed skill to $SKILL_DIR"

# ── 12. Install TUI ─────────────────────────────────────────────────────

mkdir -p "$LOSSLESS_HOME/tui"
cp "$SCRIPT_DIR/tui/lcc_tui.py"        "$LOSSLESS_HOME/tui/"
cp "$SCRIPT_DIR/tui/contracts_view.py" "$LOSSLESS_HOME/tui/"
cp "$SCRIPT_DIR/tui/lcc-tui"           "$LOSSLESS_HOME/tui/"
chmod +x "$LOSSLESS_HOME/tui/lcc-tui"
ln -sf "$LOSSLESS_HOME/tui/lcc-tui" /usr/local/bin/lcc-tui 2>/dev/null || \
ln -sf "$LOSSLESS_HOME/tui/lcc-tui" "$HOME/.local/bin/lcc-tui" 2>/dev/null || true
echo "  [ok] Installed lcc-tui to PATH"

# ── 13. Verify ──────────────────────────────────────────────────────────

echo ""
echo "Verifying installation..."

python3 -c "
import sys
sys.path.insert(0, '$LOSSLESS_HOME/scripts')
import db
conn = db.get_db()
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
expected = ['sessions', 'messages', 'summaries', 'summary_sources', 'messages_fts', 'summaries_fts', 'dream_log']
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
echo "Commands available: lcc, lcc_grep, lcc_expand, lcc_context, lcc_sessions, lcc_handoff, lcc_status, lcc_dream, lcc-tui"
echo "MCP server: see the [ok] line in section 10 above (registration depends on plugin presence)"
echo "Hooks configured for: SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompact, PreToolUse, PostToolUse"
echo ""
echo "Optional — Semantic Search (hybrid FTS5 + vector):"
echo "  pip install fastembed                       # local ONNX embeddings (~200 MB)"
echo "  Then add to config.json: \"embeddingEnabled\": true"
echo "  Run once to index existing messages: lcc reindex --embeddings"
echo ""
echo "To uninstall, remove $LOSSLESS_HOME and the hooks from $SETTINGS_FILE"
