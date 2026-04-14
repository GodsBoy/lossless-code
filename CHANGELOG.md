# Changelog

## v1.1.1 — 2026-04-14

Patch release that ships every main-branch delta accumulated since the
v1.1.0 marketplace artifact, headlined by the `claude --print` CWD fix
that closes the `claude --resume` pollution bug. Marketplace users
should upgrade to pick up the fix; manual installs already covered by
re-running `install.sh`.

### Fixed

- **`claude --print` subprocess CWD pollution** (PR #11). The
  summariser's `claude --print` calls now pin `cwd=` to
  `~/.lossless-code/.cli-cwd`, so Claude Code files internal
  summarisation sessions in their own bucket instead of polluting the
  user's `claude --resume` list with hundreds of "Summarise the
  following conversation turns concisely…" entries. Includes
  `scripts/check_summariser_pollution.py` regression detector.
- **Installer + README sync for fingerprint feature** (PR #10).
  `install.sh` now copies the new PreToolUse / PostToolUse hooks and
  the `scripts/file_context.py`, `scripts/hook_store_tool_call.py`
  files added by the fingerprint feature; README documents the new
  `fileContextEnabled` flag and the hook surface.

### Added — fingerprint file context (default off, PR #9)

- **`fileContextEnabled` config flag** (default `false`) gates all new
  behavior. Flip to `true` in `~/.lossless-code/config.json` to opt in.
- **Schema additions**: `messages.file_path`, `summaries.kind`, plus
  `idx_messages_file_path` (partial) and `idx_summary_sources_source`
  composite. Migrations are idempotent.
- **PostToolUse hook** (`hook_store_tool_call.py`) records which file a
  `Read | Edit | Write | MultiEdit | NotebookEdit` tool call touched.
- **Polarity classification** (`summarise.classify_chunk_polarity`) tags
  summaries as `created | edited | discussed | mixed` based on the
  tool calls in the chunk; propagates through cascade.
- **`db.get_summaries_for_file`** walks `summary_sources` upward from a
  message's `file_path` via recursive CTE (hop ceiling 16) and excludes
  consolidated nodes.
- **`inject_context.format_file_fingerprint`** renders a compact,
  200-token fingerprint with a truncation ladder that always preserves
  the file path and the `lcc_expand` hint.
- **PreToolUse hook** (`pre_tool_use.sh` + `scripts/file_context.py`)
  injects the fingerprint as `additionalContext` before file-touching
  tools run. Reads are served from a JSON cache under `cache/` with
  60s TTL, file-locked reads/writes, atomic replace, and single-flight
  stampede guard. Cold path opens vault.db read-only so it never blocks
  the writer.
- **`lcc_expand --file <path>`** (CLI) and `{"file": "..."}` (MCP) drill
  from a file path to the recent summaries that reference it.
- **`lcc status`** surfaces tagged message count, distinct tagged files,
  and cached fingerprint count when the flag is on.

### Changed

- **`scripts/db.py` split into `scripts/db/` package** (PR #8). Single
  745-line module replaced with focused submodules: `config`,
  `schema`, `messages`, `summaries`, `sessions`, `embeddings`,
  `search`, `dream_log`. Public import surface is preserved via
  `scripts/db/__init__.py`; existing call sites unchanged.
- **BM25 prompt-aware context eviction** (PR #7). Context injection now
  scores candidate summaries against the live prompt with BM25 instead
  of a flat recency cut, so high-signal older summaries survive
  eviction when the prompt actually references them.

### Added — earlier in the v1.1.0 → v1.1.1 window

- **Session filtering for lossless-claw v0.7.0 parity**: stateless
  session gate, stop-hook filtering, plus DB-layer tests covering
  pattern matching and filter behaviour.
- **Circuit breaker + dynamic chunk sizing for summarisation**:
  protects the summariser from runaway provider failures and adapts
  chunk size to the active provider's context window.
- **Vault bloat protection**: summary size caps and dream pagination
  prevent unbounded growth on long-running vaults.
- **`claude-cli` provider** for Claude Max / Pro subscription users
  (no API key path). `ANTHROPIC_API_KEY` is stripped from the
  subprocess env so the CLI falls back to OAuth.
- **Custom Anthropic-compatible providers** (MiniMax and others) via
  `openaiBaseUrl`, plus provider auto-detection and structured error
  handling.
- **`dream --status` flag** for inspecting dream-cycle state.
- **`scripts/check_summariser_pollution.py`** stdlib regression detector
  for the cwd pollution bug. Walks `~/.claude/projects/`, skips the
  legitimate `-root--lossless-code--cli-cwd` bucket, exits non-zero
  with a file list if any other bucket contains a polluting `.jsonl`.

## v1.1.0 — semantic search hybrid

- Hybrid semantic search combining FTS5 BM25 with vector similarity via
  reciprocal rank fusion. Cached `fastembed.TextEmbedding` instance.
