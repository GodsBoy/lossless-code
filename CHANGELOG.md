# Changelog

## v1.2.0 - 2026-04-25

The v1.2 release. Claude Code remembers across compactions and sessions.
SessionStart now emits a token-budgeted reference bundle instead of a
2-5K text dump; the agent pulls depth on demand via existing MCP tools.
Behavior contracts let you teach Claude durable preferences once and have
them ride into every session. An OpenTelemetry-shaped span substrate
underneath both features makes the message graph queryable through the
new `lcc_expand` span_id mode.

### Three composing layers

**Compaction-aware reference bundle** (the headline). `inject_context.py`
no longer dumps the full dream-patterns text plus top summaries. Instead,
SessionStart receives a slot-packed reference bundle ordered with the
recovery-protocol line first so the agent learns the recovery contract
before any content. Default cap 1000 tokens, configurable via
`bundleTokenBudget`. Each item carries a literal `Expand: call MCP tool`
instruction the agent can invoke verbatim. PreCompact and PostCompact
hooks stay no-push (`hooks/post_compact.sh:54-56` constraint preserved
verbatim). Recovery routes entirely through the SessionStart bundle's
recovery line plus agent-initiated MCP calls.

**Behavior contracts.** New `contracts` table with typed kind
(`prefer` / `forbid` / `verify-before`), bylined, append-only with a
`supersedes_id` chain. The dream cycle proposes contracts as `Pending`;
the new TUI Contracts tab (key `5`) lets you approve (`a`), reject /
retract (`r`), or supersede (`s`) interactively. Filter cycle (`t`)
flips between Pending, Active, and Retracted views. Every Active
contract rides in the SessionStart bundle's contracts slot.

**OpenTelemetry-shaped span substrate.** `messages` gains four new
NULL-able columns: `parent_message_id`, `span_kind`, `tool_call_id`,
`attributes` (JSON). Hooks populate them at write time. New
`scripts/db/spans.py` exposes `get_span`, `get_span_chain`,
`get_children_spans`, `cap_attributes_json`. `lcc_expand` accepts
`span_id` and walks the parent chain; structured-error JSON returns
(`span_not_found` / `expand_too_large` / `vault_corrupt` /
`permission_denied`) with sanitized static messages so error output
can never leak filesystem paths or library internals into agent
context.

### Added

- **Bundle token budget** (`bundleTokenBudget`, default 1000) and the
  `bundleEnabled` rollback flag (default `true`). Set
  `bundleEnabled: false` in `~/.lossless-code/config.json` to disable
  SessionStart injection entirely while keeping all other lossless-code
  features active.
- **`lcc_contracts` MCP tool and `lcc contracts` CLI verb.** Actions:
  `list`, `show`, `approve`, `reject`, `retract`, `supersede`. Same
  result shape on both surfaces; structured-error JSON for new MCP
  errors with sanitized static messages.
- **`lcc_expand span_id` mode** plus structured errors. Existing
  `summary_id` and `file` modes preserve their v1.1.x human-readable
  output for backwards compatibility.
- **TUI Contracts tab** with full state coverage: Pending / Active /
  Retracted filter cycling via `t`, ContractDetailScreen modal for the
  full body before approval, SupersedeBodyScreen TextArea modal for
  in-place revision, RetractionReasonPrompt for required-reason
  retracts, conflict-signal column when an opposing-kind Active rule
  overlaps the candidate.
- **`collect_status_dict` shared helper** in `scripts/lcc_core.py`.
  Both `lcc status` and the `lcc_status` MCP tool now route through
  this single source of truth, closing a historical CLI-vs-MCP drift
  pattern. New v1.2 fields surfaced in status: contract counts by
  status, decision count, bundle enabled state and budget,
  `lastDreamMode` (whether the dream cycle ran via LLM, regex
  fallback, or failed).

### Hardened

- **Vault permissions.** `~/.lossless-code/` now ships with `mode=0o700`
  on the directory, `0o600` on `vault.db` and `config.json`. Closes
  the world-readable gap that v1.2's new `contracts.body` and
  `messages.attributes` columns would otherwise inherit on shared
  machines.
- **Contract body sanitization at injection time.** Bundle assembler
  strips `\n` and `\r` from contract bodies and rejects bodies
  containing the literal `[lcc.contract]` marker. Closes the newline
  injection vector flagged in v1.2 doc review: without this, an
  attacker who got an approved contract body with embedded
  `\n[lcc.contract] FORBID poison` would see a synthetic second
  contract line in the SessionStart bundle.
- **Supersede atomicity.** `supersede_contract` wraps INSERT new and
  UPDATE old in `BEGIN IMMEDIATE` / `COMMIT` so concurrent CLI + MCP
  writes cannot land two rival Active rows pointing at the same
  target.
- **`attributes` JSON shape validation.** `store_message` rejects
  non-dict attributes input (logged to stderr, stored as empty
  object); `cap_attributes_json` token-caps oversize payloads with
  a tombstone preserving the size signal.

### Removed

- **Legacy v1.1 full-text injection from `inject_context.py`.** The
  reference bundle is now the only injection path. Set
  `bundleEnabled: false` to disable SessionStart injection entirely
  if you want the v1.1 behavior of "no lossless-code context layer."
  No compatMode dual-path is shipped; the previous proposal was
  rejected during plan review as a complexity tax for a measurement
  that can be done once pre-merge.

### Multi-install drift warning

`install.sh` now detects when both manual and plugin installs are
present and emits a stderr warning with one-liner remediation paths.
The `lcc doctor` self-healing tool remains deferred to v1.3.

### Migration notes

- **Existing installs**: re-run `install.sh` to pick up the two new
  scripts (`scripts/contracts.py`, `scripts/lcc_core.py`). Plugin
  users get the upgrade through the marketplace refresh.
- **First dream cycle post-v1.2**: the dream cycle now runs
  Phase 1.5 (contract + decision extraction) after pattern extraction.
  Pending contracts populate the TUI queue; nothing rides in the
  bundle until you approve.
- **Schema migrations**: idempotent `ALTER TABLE` adds five columns
  (4 on `messages` for spans, 1 on `dream_log` for `mode`) plus the
  `contracts` table. NULL-permissive for backfill compatibility;
  pre-v1.2 rows have no causal data and that is accepted.

## v1.1.1 - 2026-04-14

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

### Added

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

## v1.1.0 — semantic search hybrid

- Hybrid semantic search combining FTS5 BM25 with vector similarity via
  reciprocal rank fusion. Cached `fastembed.TextEmbedding` instance.
