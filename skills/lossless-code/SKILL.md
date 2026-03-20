---
name: lossless-code
description: DAG-based lossless context management — search, expand, and recall from your full conversation history
---

# Lossless Context Commands

You have access to a persistent, DAG-based conversation vault. Every message from every session is preserved in SQLite. Summaries form a directed acyclic graph — nothing is ever deleted.

## Available Commands

### `lcc_grep <query>` — Search everything
Full-text search across all messages and summaries in the vault.
```bash
lcc_grep "database migration"
lcc_grep "error in auth"
```

### `lcc_expand <summary_id>` — Drill into a summary
Expand a summary node back to its source messages or child summaries. Use `--full` for untruncated output.
```bash
lcc_expand sum_abc123def456
lcc_expand sum_abc123def456 --full
```

### `lcc_context [query]` — Get relevant context
Surface the top N most relevant DAG nodes for a query. Without a query, returns highest-depth summaries (broadest overview).
```bash
lcc_context "auth refactor"
lcc_context --limit 10
```

### `lcc_sessions` — List sessions
List recorded sessions with timestamps, working directories, and whether a handoff exists.
```bash
lcc_sessions
lcc_sessions --limit 5
```

### `lcc_handoff` — Session handoff
Show the handoff from a previous session, or generate one for the current session.
```bash
lcc_handoff
lcc_handoff --generate --session "$CLAUDE_SESSION_ID"
```

### `lcc_status` — Vault stats
Show message count, summary count, max depth, vault size.
```bash
lcc_status
```

## When to Use

- **Starting a new session**: Run `lcc_context` to see what happened recently.
- **Picking up where you left off**: Run `lcc_handoff` to see the previous session's summary.
- **Searching for past decisions**: Run `lcc_grep "decision about X"`.
- **Understanding a summary**: Run `lcc_expand` to drill down to the original messages.
- **Before ending a session**: Run `lcc_handoff --generate` to save a handoff for next time.

## How It Works

Messages are automatically captured by hooks on every turn. Before context compaction, unsummarised messages are chunked and summarised into DAG nodes. Summaries cascade to higher depths when they accumulate. The full chain from high-level overview → detailed summary → original message is always traversable via `lcc_expand`.
