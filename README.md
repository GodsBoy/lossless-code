# lossless-code

DAG-based Lossless Context Management for Claude Code.

Every message preserved forever in SQLite. Summaries form a directed acyclic
graph that never deletes. Active recall tools injected into every Claude Code
session.

```
                              ┌──────────────────┐
                              │   Claude Code     │
                              │   Session         │
                              └────────┬─────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
              ┌─────▼─────┐    ┌──────▼──────┐    ┌──────▼──────┐
              │  Hooks     │    │   Skills    │    │   CLI       │
              │            │    │             │    │   Tools     │
              │ SessionStart│   │ lcc_grep    │    │             │
              │ Stop       │    │ lcc_expand  │    │ lcc_status  │
              │ PreCompact │    │ lcc_context │    │             │
              │ PostCompact│    │ lcc_sessions│    │             │
              │ UserPrompt │    │ lcc_handoff │    │             │
              └─────┬──────┘    └──────┬──────┘    └──────┬──────┘
                    │                  │                   │
                    └──────────────────┼───────────────────┘
                                       │
                              ┌────────▼─────────┐
                              │    vault.db       │
                              │    (SQLite)       │
                              │                   │
                              │  messages         │
                              │  summaries        │
                              │  summary_sources  │
                              │  sessions         │
                              │  FTS indexes      │
                              └───────────────────┘
```

## What Makes This Different

Existing Claude Code memory tools (ClawMem, context-memory, context-mode,
claude-mem) use retrieval-augmented memory. lossless-code uses **DAG-based
lossless preservation**:

- **Nothing is ever deleted.** Every message stays in `vault.db`.
- **Summaries cascade.** Messages → depth-0 summaries → depth-1 summaries → ...
- **Full drill-down.** `lcc_expand` traces any summary back to original messages.
- **Automatic.** Hooks capture turns and trigger summarisation transparently.

## Install

```bash
git clone https://github.com/GodsBoy/lossless-code.git
cd lossless-code
bash install.sh
```

The installer:
1. Creates `~/.lossless-code/` with vault.db and scripts
2. Configures Claude Code hooks in `~/.claude/settings.json`
3. Installs the skill to `~/.claude/skills/lossless-code/`
4. Adds CLI tools to PATH

Idempotent — safe to run again to upgrade.

### Requirements

- Python 3.10+
- SQLite 3.35+ (for FTS5)
- Claude Code CLI

Optional: `anthropic` Python package for AI-powered summarisation (falls back
to extractive summaries without it).

## Commands

### `lcc_grep <query>`

Full-text search across all messages and summaries.

```bash
lcc_grep "database migration"
lcc_grep "auth refactor"
```

### `lcc_expand <summary_id>`

Expand a summary node back to its source messages.

```bash
lcc_expand sum_abc123def456
lcc_expand sum_abc123def456 --full
```

### `lcc_context [query]`

Surface relevant DAG nodes for a query. Without a query, returns highest-depth
summaries.

```bash
lcc_context "auth system"
lcc_context --limit 10
```

### `lcc_sessions`

List recorded sessions with timestamps and handoff status.

```bash
lcc_sessions
lcc_sessions --limit 5
```

### `lcc_handoff`

Show or generate a session handoff.

```bash
lcc_handoff
lcc_handoff --generate --session "$CLAUDE_SESSION_ID"
```

### `lcc_status`

Show vault statistics.

```bash
lcc_status
```

## How It Works

### Hooks (Automatic)

| Hook | Event | Purpose |
|------|-------|---------|
| `session_start.sh` | SessionStart | Register session, inject handoff + summaries |
| `stop.sh` | Stop | Persist each turn to vault.db |
| `user_prompt_submit.sh` | UserPromptSubmit | Surface relevant context for the prompt |
| `pre_compact.sh` | PreCompact | Run DAG summarisation before compaction |
| `post_compact.sh` | PostCompact | Record compaction, re-inject top summaries |

### DAG Summarisation

1. Collect unsummarised messages, chunk into groups of ~20
2. Summarise each chunk (via Claude API or extractive fallback)
3. Write summary nodes to `summaries` table (depth=0)
4. Link to sources in `summary_sources`
5. Mark source messages as summarised
6. If depth-N exceeds threshold: cascade to depth-N+1
7. Repeat until under threshold at every depth

### Storage

```
~/.lossless-code/
  vault.db       # SQLite — all messages, summaries, DAG, sessions
  config.json    # Settings (summary model, thresholds)
  scripts/       # Python modules and CLI tools
  hooks/         # Shell scripts called by Claude Code hooks
```

## Configuration

`~/.lossless-code/config.json`:

```json
{
  "summaryModel": "claude-haiku-4-5-20251001",
  "chunkSize": 20,
  "depthThreshold": 10,
  "incrementalMaxDepth": -1,
  "workingDirFilter": null
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `summaryModel` | `claude-haiku-4-5-20251001` | Model for summarisation |
| `chunkSize` | `20` | Messages per summary chunk |
| `depthThreshold` | `10` | Max nodes at any depth before cascading |
| `incrementalMaxDepth` | `-1` | Max cascade depth (-1 = unlimited) |
| `workingDirFilter` | `null` | Only capture messages from this directory |

## Schema

```sql
sessions      — session_id, working_dir, started_at, last_active, handoff_text
messages      — id, session_id, turn_id, role, content, tool_name, working_dir, timestamp, summarised
summaries     — id, session_id, content, depth, token_count, created_at
summary_sources — summary_id, source_type, source_id
messages_fts  — FTS5 index on messages.content
summaries_fts — FTS5 index on summaries.content
```

## Uninstall

```bash
rm -rf ~/.lossless-code
# Remove hooks from ~/.claude/settings.json manually
# Remove skill: rm -rf ~/.claude/skills/lossless-code
```

## License

MIT
