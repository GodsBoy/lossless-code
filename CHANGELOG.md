# Changelog

## Unreleased

### Added — fingerprint file context (default off)

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
