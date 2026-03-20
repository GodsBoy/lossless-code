<div align="center">

# 🧠 lossless-code

**DAG-based Lossless Context Management for Claude Code.**

*Every message preserved forever. Summaries cascade, never delete. Full recall across sessions.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![GitHub stars](https://img.shields.io/github/stars/GodsBoy/lossless-code?style=social)](https://github.com/GodsBoy/lossless-code/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/GodsBoy/lossless-code?style=social)](https://github.com/GodsBoy/lossless-code/network/members)
[![GitHub issues](https://img.shields.io/github/issues/GodsBoy/lossless-code)](https://github.com/GodsBoy/lossless-code/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/GodsBoy/lossless-code)](https://github.com/GodsBoy/lossless-code/commits/main)

[![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![SQLite](https://img.shields.io/badge/SQLite-FTS5-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-hooks-D97706?logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/GodsBoy/lossless-code/pulls)

[Getting Started](#install) · [Commands](#commands) · [How It Works](#how-it-works) · [Configuration](#configuration) · [Contributing](#contributing)

</div>

---

## The Problem

Claude Code forgets everything between sessions. Existing memory tools (ClawMem, context-memory, context-mode, claude-mem) use flat retrieval-augmented memory: keyword search over stored snippets with no structure, no hierarchy, and no way to drill from a summary back to the original conversation.

When your project spans weeks and hundreds of sessions, flat search breaks down. You get fragments without lineage.

## What Makes lossless-code Different

lossless-code uses **DAG-based lossless preservation**, the same approach pioneered by [lossless-claw](https://github.com/Martian-Engineering/lossless-claw) for [OpenClaw](https://github.com/openclaw/openclaw):

- **Nothing is ever deleted.** Every message stays in `vault.db` forever.
- **Summaries form a directed acyclic graph.** Messages become depth-0 summaries, which cascade to depth-1, depth-2, and beyond.
- **Full drill-down.** `lcc_expand` traces any summary node back to the original messages that created it.
- **Automatic.** Claude Code hooks capture every turn and trigger summarisation transparently. No manual effort.
- **Cross-session recall.** Start a new session and your full project history is immediately searchable and injectable.

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
                              │  FTS5 indexes     │
                              └───────────────────┘
```

## Install

```bash
git clone https://github.com/GodsBoy/lossless-code.git
cd lossless-code
bash install.sh
```

The installer:
1. Creates `~/.lossless-code/` with `vault.db` and scripts
2. Configures Claude Code hooks in `~/.claude/settings.json`
3. Installs the skill to `~/.claude/skills/lossless-code/`
4. Adds CLI tools to PATH

Idempotent: safe to run again to upgrade.

### Requirements

- Python 3.10+
- SQLite 3.35+ (for FTS5)
- Claude Code CLI

Optional: `anthropic` Python package for AI-powered summarisation (falls back to extractive summaries without it).

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

Surface relevant DAG nodes for a query. Without a query, returns highest-depth summaries.

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

Show vault statistics: message count, summary count, DAG depth, and FTS index health.

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
  vault.db       # SQLite: all messages, summaries, DAG, sessions
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
sessions      -- session_id, working_dir, started_at, last_active, handoff_text
messages      -- id, session_id, turn_id, role, content, tool_name, working_dir, timestamp, summarised
summaries     -- id, session_id, content, depth, token_count, created_at
summary_sources -- summary_id, source_type, source_id
messages_fts  -- FTS5 index on messages.content
summaries_fts -- FTS5 index on summaries.content
```

## Comparison

| | lossless-code | ClawMem | context-memory | claude-mem |
|---|---|---|---|---|
| **Storage** | SQLite with FTS5 | SQLite | Markdown files | JSON/SQLite |
| **Structure** | DAG (summaries cascade) | Flat retrieval | Flat retrieval | Flat retrieval |
| **Drill-down** | Full (summary to source messages) | None | None | None |
| **Auto-capture** | Hooks (zero manual effort) | Hooks | Manual | Manual |
| **Cross-session** | Yes (vault persists) | Yes | Yes | Yes |
| **Summarisation** | Cascading DAG (depth-N) | Single-level | None | Single-level |
| **Search** | FTS5 full-text | Semantic | Keyword | Semantic |

## Uninstall

```bash
rm -rf ~/.lossless-code
# Remove hooks from ~/.claude/settings.json manually
# Remove skill: rm -rf ~/.claude/skills/lossless-code
```

## Prior Art and Acknowledgements

lossless-code is a Claude Code adaptation of the **Lossless Context Management (LCM)** architecture created by [Jeff Lehman](https://github.com/jalehman) and the [Martian Engineering](https://github.com/Martian-Engineering) team. Their [lossless-claw](https://github.com/Martian-Engineering/lossless-claw) plugin for [OpenClaw](https://github.com/openclaw/openclaw) proved that DAG-based context preservation eliminates the information loss problem in long-running AI sessions. lossless-code brings that same architecture to Claude Code.

Additional references:
- [ClawMem](https://github.com/yoloshii/clawmem) by yoloshii (hooks architecture patterns)
- [Voltropy LCM paper](https://papers.voltropy.com/LCM) (theoretical foundation)

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Write tests for new functionality
4. Ensure tests pass
5. Open a pull request

## Star History

<a href="https://star-history.com/#GodsBoy/lossless-code&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=GodsBoy/lossless-code&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=GodsBoy/lossless-code&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=GodsBoy/lossless-code&type=Date" />
 </picture>
</a>

## Contributors

<a href="https://github.com/GodsBoy/lossless-code/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=GodsBoy/lossless-code" />
</a>

## Licence

MIT

---

<div align="center">

**If lossless-code helps your workflow, consider giving it a ⭐**

[Report Bug](https://github.com/GodsBoy/lossless-code/issues/new?labels=bug) · [Request Feature](https://github.com/GodsBoy/lossless-code/issues/new?labels=enhancement)

</div>
