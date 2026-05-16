---
date: 2026-04-25
topic: lossless-claude-code-compaction
focus: end-user "just remembers" + token efficiency + beats native compaction / Anthropic Memory / claude-mem
mode: repo-grounded
---

# Ideation: Make Claude Code Truly Lossless Across Sessions

## Grounding Context

lossless-code is a Python CLI + MCP server for DAG-based AI session memory in Claude Code: SQLite vault, cascading depth-N summariser, dream cycle pattern extractor, dual-distributed (Claude Code plugin + standalone install). Recent work concentrates on install-path drift, MCP/CLI parity, subprocess hygiene, vault unbounded growth (33GB / 11GB RSS / 9-min queries documented as severity:critical), and provider-agnostic LLM transport. Mid-flight branches `feat/bm25-prompt-aware-eviction` and `feat/lossless-dream` have no PRs.

**Competitive frame (refined this session):** the win condition is the end user feeling that Claude Code "just remembers" what it needs across sessions and compactions, at minimal token cost. The three things to beat or aid:

1. **Native Claude Code compaction** — 5-layer cascade (disk-spill, background notes, compaction-uses-notes, file re-read, CLAUDE.md re-inject). Strengths: zero-config, in-context, fast. Weaknesses: lossy by design, session-scoped (issue [anthropics/claude-code#34556](https://github.com/anthropics/claude-code/issues/34556) is the canonical demand signal: "59 compactions in a project, no cross-compaction continuity").
2. **Anthropic Memory** (Opus 4.7) — cross-conversation, automatic. Weaknesses: opaque blobs the user can't see/edit/query, extracts (loses lineage), per-account.
3. **claude-mem / Auto-Dream / ClawMem** — same lossy-summary architecture as Auto-Dream (per `docs/solutions/architecture-decisions/lossless-dream-pattern-extraction-and-dag-consolidation.md`). No provenance, no rules/contracts, no bisect.

External landscape: Mem0/Letta/Zep/OpenAI Memory all discard raw messages. Letta's own LoCoMo benchmark showed plain filesystem grep+vector beat Mem0's graph 74% vs 68.5% — caution against over-investing in DAG complexity. MINJA memory poisoning (>95% success, NeurIPS 2025) and GDPR Art 17/20 are active risks against append-only memory.

## Codex Extension, 2026-05-16

The same "just remembers" problem now applies to Codex. The repo already tracks Codex CLI as planned in README compatibility notes: MCP recall works today, but automatic capture needs Codex hook adaptation. Local Codex CLI inspection on this machine showed `codex-cli 0.130.0-alpha.5`; `hooks` is stable, `plugins` and `mcp` are stable, while `plugin_hooks` remains under development and disabled. That makes the right opening move MCP-first plus user-level hooks, not a plugin-native hook dependency.

Current OpenAI docs also sharpen the context-window framing. [`codex-mini-latest`](https://developers.openai.com/api/docs/models/codex-mini-latest), described as optimized for Codex CLI, lists a 200,000 context window and 100,000 max output tokens. Newer Codex API model pages such as [GPT-5.3-Codex](https://developers.openai.com/api/docs/models/gpt-5.3-codex) list a 400,000 context window and 128,000 max output tokens. The product point is not "Codex has no context"; it is that even 200k to 400k tokens remains finite, expensive to refill, and lossy when work spans many sessions, repos, branches, terminals, and app connectors.

**Codex-specific win condition:** make Codex feel continuous across local threads, app sessions, CLI sessions, goals, compactions, and agent handoffs, with recall that is inspectable and pull-on-demand instead of stuffing the whole past into every turn.

### Codex Idea A. Codex MCP-first recall pack
**Description:** Ship a Codex-ready MCP registration path before waiting on plugin hooks. The first Codex support slice should make `lcc_grep`, `lcc_expand`, `lcc_context`, `lcc_sessions`, `lcc_handoff`, `lcc_status`, and `lcc_contracts` available through `codex mcp add`, with a `lcc codex doctor` check that verifies the MCP server is registered and can read the same vault.
**Warrant:** `direct:` README already says MCP works everywhere today and Codex CLI is planned. `direct:` local `codex mcp list` returned no configured MCP servers, so there is immediate setup friction. `external:` [OpenAI Codex use cases](https://developers.openai.com/codex/use-cases/) explicitly include "Create a CLI Codex can use" and "Understand large codebases," which fit an MCP-first recall surface.
**Rationale:** This creates value even if automatic capture is not ready. A Codex user can ask for prior context and fetch exact source history without any hook dependency. It also gives implementation a narrow, testable first PR.
**Downsides:** Recall is manual until hooks land. Users may forget to call the MCP tools unless SessionStart or rules nudge them.
**Confidence:** 90%
**Complexity:** Low
**Status:** New

### Codex Idea B. Codex hook adapter with event parity map
**Description:** Add a Codex hook adapter that maps Codex's stable hook surface to the existing capture scripts. Start with the events local Codex already exposes reliably, then map them to the closest current scripts: session start creates or resumes a session and emits the reference bundle, prompt submission stores user input and optional context hints, stop captures transcript or final assistant messages. Treat compaction hooks as opportunistic until Codex exposes an exact PreCompact and PostCompact equivalent.
**Warrant:** `direct:` local `codex features list` shows `hooks` as stable, while `plugin_hooks` is under development. `direct:` existing hook scripts already separate session start, user prompt, stop, pre-compact, and post-compact behavior. `direct:` README currently lists Codex CLI as planned, with SessionStart, Stop, and UserPromptSubmit named.
**Rationale:** This opens the "automatic capture" loop without forcing the Claude Code plugin model onto Codex prematurely. It also keeps the implementation honest: one adapter layer normalizes event payloads into existing Python script arguments.
**Downsides:** Codex hook payload shapes may differ enough to require new parser tests. Without plugin hooks, installation may need user-level config edits first.
**Confidence:** 80%
**Complexity:** Medium
**Status:** New

### Codex Idea C. Context-window aware bundle budgets
**Description:** Make `bundleTokenBudget` model-aware for Codex instead of a single 1000-token default. Detect or let users configure the active Codex model class, then choose a conservative bundle budget: tiny for `codex-mini-latest`, larger for GPT-5.x Codex models, and always bounded as a percentage of context. The bundle should report its own size and dropped sections so users can see what was omitted.
**Warrant:** `external:` OpenAI docs list 200,000 context for `codex-mini-latest` and 400,000 for GPT-5.3-Codex. `direct:` README already has a fixed `bundleTokenBudget` default. `direct:` the previous provider-agnostic ideation already identified model-aware chunking as a needed direction.
**Rationale:** Codex support should not assume "latest" means unlimited. A 200k context session can still be burned by long diffs, logs, screenshots, connector payloads, and tool outputs. Budgeting by model keeps Lossless-Code from becoming the thing that consumes the context it is supposed to protect.
**Downsides:** Active-model detection may be incomplete in hooks. A static map needs maintenance as Codex models change.
**Confidence:** 85%
**Complexity:** Low-Medium
**Status:** New

### Codex Idea D. Goal and thread continuity bridge
**Description:** Treat Codex goals, thread resumes, and forks as first-class session continuity events. Store goal objective, goal status, branch, cwd, active files, and last verification story as structured records. On SessionStart or `lcc_context`, surface "what goal is this thread pursuing, what changed, what remains" with drill-down links to prior sessions.
**Warrant:** `direct:` local Codex config has goals enabled, and the [Codex use case page](https://developers.openai.com/codex/use-cases/) lists "Follow a goal" as a durable-workflow use case. `direct:` Lossless-Code already stores sessions, summaries, decisions, handoff state, and contracts.
**Rationale:** Codex has a stronger long-running task shape than simple chat. Lossless-Code can be the continuity layer that explains why this branch exists, what was tried, and what the next safe step is after a resume or compaction.
**Downsides:** Requires payload access to goal/thread metadata or a local side-channel. If Codex does not expose goal internals, the first version may need manual CLI annotations.
**Confidence:** 75%
**Complexity:** Medium
**Status:** New

### Codex Idea E. Codex transcript importer for immediate value
**Description:** Build `lcc import-codex` to ingest existing Codex session logs from `~/.codex/sessions` and index them into the same vault. Preserve source path, thread id, cwd, model, and timestamp where available. This gives current Codex users retrospective recall before any hook work is finished.
**Warrant:** `direct:` this machine has a populated `~/.codex/sessions` directory and a `session_index.jsonl`. `direct:` existing `hook_stop.py` already parses transcript-style records for Claude Code.
**Rationale:** Importers are safer than hooks because they are offline and easy to test against fixtures. They also make the first demo stronger: install Lossless-Code, import your Codex past, ask what happened on a prior task.
**Downsides:** Session file formats may change. Needs strict redaction and permissions because Codex logs can contain sensitive local work.
**Confidence:** 80%
**Complexity:** Medium
**Status:** New

### Codex Idea F. Codex skill and AGENTS.md bootstrap
**Description:** Provide a Codex-specific skill plus AGENTS.md snippet that teaches the agent when to call `lcc_context`, `lcc_grep`, and `lcc_expand`, and when not to. Keep it instruction-light: "search before repeating investigation," "expand cited summaries before relying on them," and "do not inject huge memory dumps."
**Warrant:** `direct:` Codex already loads AGENTS.md and skills in this environment. `direct:` Lossless-Code already has a `skills/lossless-code/SKILL.md` Claude-facing skill.
**Rationale:** Until Codex can receive automatic plugin-hook context everywhere, the skill and AGENTS bootstrap are the cheapest way to change agent behavior. It also prevents the MCP-first version from feeling hidden.
**Downsides:** Instruction-only behavior is weaker than hooks. Bad instructions can cause over-searching and token waste.
**Confidence:** 80%
**Complexity:** Low
**Status:** New

### Codex Idea G. Cross-agent vault with source namespacing
**Description:** Add an explicit `agent_source` or `runtime` dimension to sessions and messages: `claude-code`, `codex-cli`, `codex-app`, `copilot-cli`, `opencode`, and so on. The same vault can hold multiple agent histories, but queries can filter by source or intentionally merge them.
**Warrant:** `direct:` README compatibility table already looks beyond Claude Code. `direct:` current schema has sessions and working directories, but no durable source runtime field.
**Rationale:** The long-term product should be "lossless context for coding agents," not a pile of per-agent forks. Namespacing prevents Codex and Claude histories from polluting each other while still allowing cross-agent recall when a project moves between tools.
**Downsides:** Schema migration touches central tables. Every hook/importer must set the source correctly.
**Confidence:** 85%
**Complexity:** Medium
**Status:** New

## Codex Recommended Path

Do not start by trying to fully port the Claude Code plugin. Start with a Codex support ladder:

1. **MCP recall now:** document and implement a Codex MCP registration path plus `lcc codex doctor`.
2. **Offline import next:** add `lcc import-codex` for existing `~/.codex/sessions` recall.
3. **User-level hooks after that:** normalize Codex stable hook payloads into the existing Python capture scripts.
4. **Model-aware bundles in parallel:** make bundle budgets aware of 200k and 400k Codex model contexts.
5. **Plugin-native hooks later:** once `plugin_hooks` is stable, package automatic capture as a Codex plugin path.

**Codex narrative:** "Codex has a large context window, but not a permanent one. Lossless-Code gives Codex durable, inspectable project memory that survives threads, goals, resumes, forks, and compaction without flooding every turn."

## Ranked Ideas

### 1. Compaction-aware context bundle: hijack PreCompact + PostCompact for lossless round-trips
**Description:** At PreCompact, lossless-code snapshots full conversation state to the DAG with span IDs. Native compaction then runs and produces its lossy summary. At PostCompact, lossless-code injects a tiny *reference bundle* — the smallest set of `lcc_expand`-able pointers the agent needs to recover anything compaction dropped. SessionStart does the same for cross-session recall: a token-budgeted bundle of contracts (rules) + recent-decisions + active-files, with every other byte fetchable on demand via MCP. The agent feels like it never lost anything; the in-context cost is bounded.
**Warrant:** `direct:` GitHub anthropics/claude-code#34556 ("59 compactions in a project, no cross-compaction continuity") is the canonical user-pain demand signal. `direct:` `hooks/` already exposes PreCompact/PostCompact; `mcp/server.py` already has `lcc_expand`; `inject_context.py` already does SessionStart injection (currently with raw pattern text, not a token-bounded reference bundle). The wiring is there; the *budget discipline + lossless-by-fetch* contract isn't.
**Rationale:** Direct competitive answer to all three: native compaction is lossy by design (bundle plus on-demand expansion makes it lossless from agent POV); Anthropic Memory stores opaque blobs (every node here is browsable, citable, retractable); claude-mem uses lossy summaries (fetch-on-demand with raw retention is the structural counter-move). Token math: today's auto-injected dream patterns dump full text (~2-5K tokens). A reference bundle (contracts ID + 5-10 span pointers) is ~500 tokens. The agent pulls depth via `lcc_expand` only when it needs it — pull-cost replaces always-paid push-cost.
**Downsides:** Bundle assembly is the new hot path; needs careful budget discipline. PostCompact timing is tight — must not block the model. Quality of bundle depends on a "what does the agent need next" heuristic that does not yet exist.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Explored

### 2. Behavior-contract enforcement layer with retractable, bylined rules
**Description:** Reframe lossless-code from "memory tool" to "behavior-contract enforcement layer." Patterns become typed, versioned rules (`prefer:`, `forbid:`, `verify-before:`) the agent commits to follow. Dreams propose contracts → human approves → SessionStart injects them as constraints. Each carries a byline + dateline + append-only retraction trail (NtM-style invalidation overlay). Contracts ride inside the bundle from #1 — they're the rules layer of the bundle.
**Warrant:** `direct:` `tasks/lessons.md` already operates as soft contracts ("NEVER use em dashes," "NEVER skip the trigger phrase"). `external:` ESLint, OpenAPI, Pact contracts demonstrate that machine-checkable rules are a 10× amplifier over advice; AP/Reuters retraction protocols show append-only correction-with-trail beats silent edits. `reasoned:` MINJA poisoning is much harder against typed rules requiring human approval.
**Rationale:** Memory tools are crowded; behavior-contract tools for AI coding agents are an empty category. The "Claude never re-asks me about my preferences" feeling. Anthropic Memory cannot do retractable + auditable. Closes recursive-summarisation hallucination *socially* rather than algorithmically.
**Downsides:** Identity reframe loses "lossless memory" positioning. Needs an approve/reject UI (likely the TUI). Confidence: 70%, Complexity: High.
**Status:** Unexplored

### 3. `lcc bisect <claim>` over conversation history
**Description:** New MCP+CLI subcommand that binary-searches the session timeline asking the LLM "was this claim true at session N's end?" and returns the session where the claim flipped. ~200 LOC on existing primitives.
**Warrant:** `external:` Cross-domain analogy named explicitly as unexploited. `direct:` `db/sessions.py` returns ordered sessions; `call_llm()` exists; DAG-with-timestamps is what bisect needs. `reasoned:` Mem0/Letta/Zep/Codex Memory cannot answer "when did what I know about X *change*" — they discard the raw stream.
**Rationale:** Lineage moat made consumer-visible. Forcing function for testing hallucination. Photogenic killer feature for v1.2 demo.
**Downsides:** LLM cost per bisect step (needs caching). Bisect quality bounded by per-step verdict reliability.
**Confidence:** 75%
**Complexity:** Low
**Status:** Unexplored

### 4. OpenTelemetry-shaped span substrate (parent_id, span_kind, attributes at message level)
**Description:** Reshape `messages` from flat turns to causally-ordered spans: add `parent_message_id`, `tool_call_id`, `span_kind`, `attributes` JSON. Hooks already see this structure (`hook_store_tool_call.py`) and discard it — capture it. The *enabler* for #1 (precise span IDs to expand against), #2 (contracts attach to spans), and #3 (bisect needs causal ordering).
**Warrant:** `external:` Grounding cross-domain analogies — *"OpenTelemetry spans: causally-ordered DAG with parent IDs... direct structural match... unexploited."* `direct:` `db/schema.py:13-24` shows messages have `turn_id` only.
**Rationale:** Highest-leverage architectural move. Converts a bespoke schema problem into a standards problem and pulls in millions of hours of OTel tooling. Without this, #1 degrades to flat-message references.
**Downsides:** Schema migration touches the most central table (careful backfill needed). Risk of becoming a generic observability tool.
**Confidence:** 70%
**Complexity:** Medium
**Status:** Unexplored

### 5. Drop pre-computed summarisation; switch to hybrid-search-on-raw with lazy demand-time compression
**Description:** Stop pre-computing the depth-N summary DAG. Vault keeps raw + embeddings; `lcc_context` retrieves raw turns by hybrid rank, compresses *only the selected window* on demand. `summarise.py`, most of `dream.py`, `summary_sources`, and `lcc_expand` collapse. Codebase ~4.4K → ~2K LOC.
**Warrant:** `external:` Letta LoCoMo benchmark — filesystem grep+vec beat Mem0 graph 74% vs 68.5%. `direct:` `db/` package already at 800-line cap because orchestration leaks. `reasoned:` recursive-summarisation hallucination compounds.
**Rationale:** Most disruptive proposal. Re-litigate after #1 lands — the bundle's reference-and-expand architecture may obviate pre-computed summaries naturally.
**Downsides:** Throws away substantial existing code. Higher per-query LLM cost without aggressive caching.
**Confidence:** 55%
**Complexity:** High
**Status:** Unexplored — deferred until #1 ships

### 6. Contact-tracing provenance graph + write-time secret redaction
**Description:** Two composing moves: typed `provenance_event` per untrusted-input ingestion + `hook_store_message.py` redacting secrets/injection-markers before insertion. Together: poisoned source → graph traversal returns exact set of summaries to invalidate.
**Warrant:** `external:` MINJA NeurIPS 2025 (>95% poisoning success); WHO contact tracing. `direct:` `inject_context.py:_sanitize_for_context` already concedes read-side untrust.
**Rationale:** Essential for enterprise (Interloom-shaped) adoption. GDPR Art 17 answer. Lower priority for individual-user "just remembers" feel — defer until #1, #2, #4 land.
**Downsides:** False-positive redactions; trust-tier classification adds a column users must trust.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored — deferred

### 7. MANIFEST.json + `lcc doctor` post-install verifier
**Description:** Generate `MANIFEST.json` listing every installable artifact (path, sha256). `lcc doctor` walks every install location and reports hash mismatches.
**Warrant:** `direct:` `tasks/lessons.md` 2026-04-14 multi-install drift. `direct:` `install.sh:23-46` already hand-lists every script.
**Rationale:** Closes the "did the fix actually ship?" class structurally. Pure ops hygiene — ship it but not as a meeting topic now.
**Downsides:** Manifest sync discipline; "developer mode" escape needed.
**Confidence:** 90%
**Complexity:** Low
**Status:** Unexplored — ship as hygiene PR, not strategy meeting

### 8. TUI as default surface; collapse the six `lcc_*` shell wrappers behind it
**Description:** `lcc` (no arg) launches the TUI; subcommands become hidden. README gets screenshot/asciinema demo.
**Warrant:** `direct:` `tui/lcc_tui.py` exists, tested, undocumented. `direct:` `install.sh:55-91` lists exactly seven `lcc_*` wrappers.
**Rationale:** UX inspector for the bundle (#1) and contracts (#2) — ships alongside, not as headline. Most marketable artifact for v1.2.
**Downsides:** Power users on remote SSH may resent the default flip.
**Confidence:** 85%
**Complexity:** Low
**Status:** Unexplored — ships alongside #1/#2

## Recommended Path

Build #1 (compaction-aware bundle) as the v1.2 headline, with #4 (OTel spans) as the enabling substrate landing in the same release. #2 (behavior contracts) ships as the bundle's rules layer. #3 (`lcc bisect`) is the demo feature that proves "we kept the raw stream." #8 (TUI) ships as the inspector. #5, #6, #7 deferred or shipped as hygiene PRs.

**v1.2 narrative:** "lossless-code makes Claude Code's compaction lossless, makes its preferences durable, and makes its history queryable — at a fraction of the token cost of pushing it all into context."

## Rejection Summary (selected — full set in `/tmp/compound-engineering/ce-ideate/3c1057ea/raw-candidates.md`)

| # | Idea (frame) | Reason |
|---|---|---|
| Pain-2 | Vault size budget | Subsumed by future bundle architecture (raw bytes don't grow context) |
| Pain-3 | MCP/CLI parity wire | Subsumed by #4 OTel substrate cleanup |
| Pain-4 / Pain-6 / Pain-8 | Branch hygiene / silent except / 0o600 vault | True but cosmetic; ship without a meeting |
| Pain-7 / Lev-6 | claude_subprocess wrapper / hook test harness | Important but executable without strategy meeting |
| Inv-1 / Inv-2 | Single zipapp / Remove install.sh | Subsumed by #7 (manifest+verifier is the structural step) |
| Inv-4 | Reactive dream cycle | Conditional on #5 outcome |
| Inv-8 | Content-addressed summary IDs | Meaningless if #5 lands |
| Refr-1 / Refr-2 | Artifact-of-record / memory-for-teacher | Absorbed into #2 |
| Refr-4 | Per-user cross-machine sync | Real demand but heavy product surface; revisit after substrate |
| Refr-6 | Vault-as-a-service daemon | Natural follow-up to #4 substrate |
| Refr-7 | Dream-as-product | Conditional on #5 |
| Lev-1 | lcc_core dispatch layer | Absorbed into implementation of any structural change |
| Lev-3 / Lev-4 / Lev-7 / Lev-8 | --json mode / flag registry / bidirectional links / llm_calls log | Compose with #1/#2/#6 or ship as PR |
| Anl-1 / Anl-3 / Anl-5 / Anl-7 | CVR/FDR / library authority / RAW+XMP / sourdough discard | Compose with #4 substrate or #6 provenance |
| Anl-2 | M&M conference dream | Conditional on #5 |
| Cnst-1 | Public-readable CDN mirror | Pipeline value absorbed into #6 |
| Cnst-3 | 100MB hard cap + BM25 eviction | Resurrects dormant branch; revisit if #5 rejected |
| Cnst-4 | N concurrent agents | Absorbed by #4 (parent IDs naturally partition) |
| Cnst-5 | Search-that-mutates / usage signal | Cheap signal feeds #1's bundle assembly heuristic |
| Cnst-6 | No-LLM vault | Already provoked by #5 |
| Cnst-7 | Pay-per-message capture | Better as a flag inside #2 contracts |
| Cnst-8 | Browser extension | Thought-experiment value absorbed; revisit when sync is real |
