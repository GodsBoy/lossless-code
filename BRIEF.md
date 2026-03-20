# lossless-code — Nehemiah Build Brief

**Project:** `lossless-code`
**Goal:** Build a DAG-based Lossless Context Management (LCM) system for Claude Code — the first of its kind.
**Priority:** High
**Model:** Opus, effort: high
**GitHub repo:** Create new public repo `GodsBoy/lossless-code` (MIT license)
**Working dir:** `/root/clawd/projects/lossless-code`

---

## Background

lossless-claw (github.com/Martian-Engineering/lossless-claw) does DAG-based context management for OpenClaw. Nobody has built an equivalent for Claude Code. All existing Claude Code memory tools (ClawMem, context-memory, context-mode, claude-mem) use retrieval-augmented memory — NOT lossless DAG preservation. This is an open gap.

lossless-code fills that gap: every message preserved forever in SQLite, DAG summarisation that never deletes, and active recall tools injected into every Claude Code session.

Academic basis: Voltropy's LCM paper (papers.voltropy.com/LCM) and the Feb 2026 arxiv paper "Contextual Memory Virtualisation: DAG-Based State Management and Structurally Lossless Trimming for LLM Agents."

---

## Architecture

### Storage

```
~/.lossless-code/
  vault.db          # SQLite — messages, summaries, DAG nodes, sessions
  config.json       # summaryModel, thresholds, working dir filter
```

### Claude Code Integration

Two integration points:

1. **Hooks** (`~/.claude/hooks/`) — fire automatically during sessions:
   - `SessionStart` → inject latest handoff + top relevant DAG summaries
   - `UserPromptSubmit` → surface relevant summaries before Claude sees prompt
   - `Stop` → persist full turn (prompt + response) to SQLite
   - `PreCompact` → trigger DAG summarisation of all unsummarised turns
   - `PostCompact` → record compaction event, link to DAG

2. **Skills** (`~/.claude/skills/lossless-code/`) — tools Claude can invoke:
   - `lcc_grep` — full-text search across all messages and summaries
   - `lcc_expand` — expand a summary node back to its source messages
   - `lcc_context` — surface top N relevant DAG nodes for current query
   - `lcc_sessions` — list sessions with metadata and handoff text
   - `lcc_handoff` — show/generate handoff for current session

---

## SQLite Schema

```sql
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  working_dir TEXT,
  started_at INTEGER,
  last_active INTEGER,
  handoff_text TEXT
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_id TEXT,
  role TEXT NOT NULL, -- 'user' | 'assistant' | 'tool'
  content TEXT NOT NULL,
  tool_name TEXT,
  working_dir TEXT,
  timestamp INTEGER NOT NULL,
  summarised INTEGER DEFAULT 0,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE summaries (
  id TEXT PRIMARY KEY, -- e.g. sum_abc123
  session_id TEXT,      -- NULL = cross-session rollup
  content TEXT NOT NULL,
  depth INTEGER NOT NULL DEFAULT 0,
  token_count INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE summary_sources (
  summary_id TEXT NOT NULL,
  source_type TEXT NOT NULL, -- 'message' | 'summary'
  source_id TEXT NOT NULL,   -- message.id or summary.id
  FOREIGN KEY (summary_id) REFERENCES summaries(id)
);

CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id);
CREATE VIRTUAL TABLE summaries_fts USING fts5(content, content=summaries, content_rowid=rowid);
```

---

## DAG Summarisation Logic

On `PreCompact` (or when `token_count(unsummarised messages) > threshold`):

1. Collect all messages where `summarised = 0`, ordered by timestamp
2. Chunk into groups of ~20 turns
3. For each chunk, call the summary model with: `"Summarise these conversation turns concisely, preserving all key decisions, facts, and outputs. Do not omit anything actionable."`
4. Write summary node to `summaries` table (depth=0)
5. Write `summary_sources` rows linking summary to source messages
6. Mark source messages as `summarised = 1`
7. If depth-0 summaries exceed threshold (e.g. 10+ nodes): run pass 2, summarising summaries into depth-1 nodes
8. Cascade until fewer than threshold nodes remain at top level

`incrementalMaxDepth = -1` means cascade indefinitely.

---

## Hooks Implementation

Hooks are shell scripts placed in `~/.claude/hooks/`. They receive JSON on stdin and can output JSON to influence Claude.

### `stop.sh` (persists each turn)
```bash
#!/bin/bash
# Persist turn to SQLite on every Stop event
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))")
# ... write to vault.db
```

### `pre_compact.sh` (triggers DAG summarisation)
```bash
#!/bin/bash
# Run DAG summarisation pass before Claude compacts
python3 ~/.lossless-code/scripts/summarise.py --session "$SESSION_ID"
```

### `session_start.sh` (injects context)
```bash
#!/bin/bash
# Output handoff + top summaries as additional context
python3 ~/.lossless-code/scripts/inject_context.py --session "$SESSION_ID" --dir "$WORKING_DIR"
```

---

## Skills

Install into `~/.claude/skills/lossless-code/` as a Claude Code skill directory with a SKILL.md.

The skills expose `lcc_grep`, `lcc_expand`, `lcc_context`, `lcc_sessions`, `lcc_handoff` as shell commands. Claude invokes them naturally when it needs to recall past context.

---

## Cross-Platform Support

- **Linux (VPS):** Primary. systemd optional watcher for embed timer if vector search added later.
- **macOS:** Full support — same SQLite, same hooks, same skills.
- **Windows (WSL):** Hooks run in WSL, vault.db in WSL fs. Full support via WSL.
- **Windows (native):** Not in v1. Out of scope initially.

Session keying uses `CLAUDE_SESSION_ID` env var (set automatically by Claude Code).
Working dir keyed from Claude Code's working directory.

---

## Installer

Create `install.sh`:
```bash
#!/bin/bash
# 1. Init ~/.lossless-code/ with vault.db schema
# 2. Copy hooks to ~/.claude/hooks/
# 3. Copy skills to ~/.claude/skills/lossless-code/
# 4. Write default config.json
# 5. Confirm with lcc status
```

Idempotent — safe to run again to upgrade.

---

## Reference Implementations to Study

- **lossless-claw source:** github.com/Martian-Engineering/lossless-claw (TypeScript, DAG logic in `src/`)
- **Volt agent:** github.com/Martian-Engineering/volt (their own agent using LCM)
- **ClawMem:** github.com/yoloshii/clawmem (hooks architecture, good reference for Stop/PreCompact patterns)
- **context-memory:** github.com/ErebusEnigma/context-memory (simple PreCompact hook reference)
- **Claude Code hooks docs:** code.claude.com/docs/en/hooks

---

## Existing Global Skills to Leverage

The following skills are already installed in `~/.claude/skills/` and should be used where relevant:
- `systematic-debugging` — for debugging hook failures
- `verification-before-completion` — ensure each component is verified before moving on
- `security-best-practices` — SQLite path traversal, hook input sanitisation
- `gh-fix-ci` / `gh-address-comments` — for PR workflow after pushing to GitHub
- `skill-creator` — when packaging the final Claude Code skill directory

---

## Deliverables

1. `~/.lossless-code/vault.db` initialised with schema
2. `~/.lossless-code/config.json` with sensible defaults
3. `~/.lossless-code/scripts/` — Python/bash scripts for summarise, inject_context, grep, expand
4. `~/.claude/hooks/` — stop.sh, pre_compact.sh, session_start.sh, user_prompt_submit.sh
5. `~/.claude/skills/lossless-code/` — SKILL.md + all lcc_* commands
6. `install.sh` — idempotent installer
7. `README.md` — clear, with architecture diagram in ASCII, install steps, config reference
8. GitHub repo `GodsBoy/lossless-code` with all files pushed
9. Initial test: run `lcc_grep "test"` and confirm it returns results from current session

---

## Git & Attribution Rules

- Commit as: `GodsBoy <dhuysamen@gmail.com>`
- No AI mentions in commits, PR body, or README
- MIT license
- No `Co-Authored-By` lines

---

## Completion Signal

Print exactly: `RALPH_COMPLETE` when all deliverables are done and verified.

---

## Key Design Principle

**Nothing is ever deleted.** Every message ever written stays in `vault.db`. Summaries link back to their sources. If Claude needs detail from 3 months ago, `lcc_expand` retrieves it in full. This is the core promise — and what separates lossless-code from every other Claude Code memory tool.
