# TUI Reference

The lossless-code TUI (`lcc-tui`) is an interactive terminal application for inspecting your vault: sessions, messages, summaries, and search. Built with [Textual](https://textual.textualize.io/) (Python).

## Installation

`lcc-tui` is installed automatically by `install.sh` or the Claude Code plugin. To install manually:

```bash
pip install textual
ln -sf /path/to/lossless-code/tui/lcc-tui /usr/local/bin/lcc-tui
```

## Quick Start

```bash
lcc-tui                    # default: ~/.lossless-code/vault.db
LOSSLESS_VAULT_DIR=/path   # custom vault location (env var)
```

## Navigation Model

The TUI uses a tabbed layout with drill-down modals:

```
Tabs: Sessions | Search | Summaries | Stats
         |          |         |
    [Enter]    [Enter]   [Enter]
         |          |         |
   Session     Jump to   Summary
   Detail      Message   Detail
```

### Global Keybindings

| Key | Action |
|-----|--------|
| `1` | Switch to Sessions tab |
| `2` | Switch to Search tab |
| `3` | Switch to Summaries tab |
| `4` | Switch to Stats tab |
| `/` | Open search modal |
| `q` | Quit |

### Sessions Tab

Lists all sessions sorted by most recent activity. Columns: session name, created time, message count.

| Key | Action |
|-----|--------|
| `Up`/`Down` | Move cursor |
| `Enter` | Open session detail |

### Session Detail (Modal)

Shows all messages for the selected session in chronological order. Messages are colour-coded by role:

- **Green** - user messages
- **Cyan** - assistant messages

Each message shows timestamp and role above the content.

| Key | Action |
|-----|--------|
| `Up`/`Down` | Scroll |
| `Escape` | Back to sessions |

### Search Tab

FTS5 full-text search across all messages in the vault. Type a query and results appear with session name, timestamp, and content snippet.

| Key | Action |
|-----|--------|
| `Enter` (on result) | Jump to that message's session |
| `/` | Open search from any tab |

### Summaries Tab

Browse DAG summaries if any exist. Shows summary ID, depth, token count, and content preview.

| Key | Action |
|-----|--------|
| `Up`/`Down` | Move cursor |
| `Enter` | Open summary detail with full content and source messages |

### Summary Detail (Modal)

Shows full summary content and lists source messages that were summarised.

| Key | Action |
|-----|--------|
| `Escape` | Back to summaries |

### Stats Tab

Dashboard showing vault statistics:
- Total sessions
- Total messages
- Total summaries
- Vault file size
- Oldest session
- Newest session

## Comparison with lcm-tui

| | lcc-tui | lcm-tui |
|---|---------|---------|
| **For** | lossless-code (Claude Code) | lossless-claw (OpenClaw) |
| **Database** | ~/.lossless-code/vault.db | ~/.openclaw/lcm.db |
| **Stack** | Python + Textual | Go + BubbleTea |
| **Navigation** | Tabbed + modals | Drill-down hierarchy |
| **Write ops** | Read-only | Repair, rewrite, dissolve, transplant |

Both can be installed simultaneously with no conflicts.
