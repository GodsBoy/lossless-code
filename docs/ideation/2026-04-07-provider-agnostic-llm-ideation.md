---
date: 2026-04-07
topic: provider-agnostic-llm
focus: Ensure lossless-code works with whatever LLM Claude Code is using
---

# Ideation: Provider-Agnostic LLM Support

## Codebase Context

**Project shape:** Python 3.10+ CLI/plugin for Claude Code. SQLite + FTS5 vault, DAG-based summaries, hooks for lifecycle events, MCP server for read-only vault access, optional fastembed-based semantic search.

**Current LLM architecture:**
- `call_llm()` in `summarise.py` is the single dispatch point for all LLM calls (generation)
- Two providers: Anthropic (default) and OpenAI, selected via `summaryProvider` config key
- Default model: `claude-haiku-4-5-20251001` for both `summaryModel` and `dreamModel`
- Config is flat keys in `~/.lossless-code/config.json` — shallow merge, no nesting
- `_get_anthropic_auth()` resolves auth from 3 sources: `ANTHROPIC_API_KEY`, OAuth credentials.json, `CLAUDE_CODE_OAUTH_TOKEN`
- `anthropicBaseUrl` config key allows custom Anthropic-compatible endpoints (MiniMax, proxies)
- Commit `6227312` recently added custom Anthropic-compatible provider support
- OpenAI path has no `base_url` override — only works with official OpenAI API
- Extractive fallbacks exist when no LLM is reachable (keyword heuristic, not real summarisation)
- Embedding layer (`embed.py`) has separate `detect_provider()` with its own if/elif chain
- `detect_provider()` conflates generation capability with search capability (known architectural issue)
- `db.py` is at 745 lines (near 800 hard max)

**Key pain points:**
1. Hardcoded Haiku default — users on non-Anthropic setups must manually edit config.json
2. No auto-detection of what provider/model is available
3. `call_llm()` silently returns empty string on ANY exception (`except Exception: pass`)
4. Dream pattern parsing (`_parse_pattern_response`) uses fragile regex that breaks across models
5. Fixed `max_tokens=2048` and `chunkSize=20` regardless of model context window
6. OpenAI provider path has no `base_url` support (can't reach Ollama, Groq, Together, etc.)

**Institutional learnings (from docs/solutions/):**
- `call_llm()` is the provider-agnostic transport layer — never call provider APIs directly from feature code
- Config keys must stay flat (shallow merge in `load_config()`)
- Always implement extractive/heuristic fallbacks when LLM APIs are unavailable
- Bare `except Exception: pass` is forbidden — distinguish auth errors from transient errors
- ML model instances must be cached at module level (singleton pattern)
- MCP parity is mandatory — CLI changes must have corresponding MCP tool updates
- `detect_provider` design issue is unresolved (conflates generation and search capability)

## Ranked Ideas

### 1. OpenAI-Compatible Universal Backend (`openaiBaseUrl`)

**Description:** Add an `openaiBaseUrl` config key (mirroring the existing `anthropicBaseUrl`) so the OpenAI provider path in `call_llm()` can target any OpenAI-compatible endpoint: Ollama, LM Studio, vLLM, Together AI, Groq, Fireworks, Azure OpenAI, etc. Also add `openaiBaseUrl` support to `_openai_embed()` in `embed.py`. Keep the Anthropic-native path for Anthropic-specific features (extended thinking blocks, prompt caching). This single change turns a 2-provider system into a dozens-provider system.

**Rationale:** The OpenAI chat completions API is the de facto standard wire protocol. Most alternative providers expose compatible endpoints. The existing OpenAI code path in `call_llm()` is already simpler (8 lines vs 18 for Anthropic). Adding `base_url` support is literally one constructor parameter. Commit `6227312` already shows users accessing non-standard endpoints through `anthropicBaseUrl` — the OpenAI side should match. A user running Ollama locally gets summarisation with no API keys at all.

**Downsides:** Anthropic-specific features (thinking blocks, prompt caching) won't work through the OpenAI path. Users still need to know their endpoint URL. Two config keys for base URLs (`anthropicBaseUrl` + `openaiBaseUrl`) may confuse users.

**Confidence:** 90%
**Complexity:** Low
**Status:** Explored (2026-04-07)

### 2. Auto-Detect Provider from Environment

**Description:** Replace the hardcoded `summaryProvider: "anthropic"` default with environment-sniffing auto-detection. On first LLM call, check for available credentials in priority order: `ANTHROPIC_API_KEY` -> Anthropic, `OPENAI_API_KEY` -> OpenAI, `OLLAMA_HOST` or localhost:11434 -> Ollama via OpenAI-compatible path, OAuth credentials -> Anthropic via proxy. Add `LOSSLESS_SUMMARY_PROVIDER`, `LOSSLESS_SUMMARY_MODEL`, and `LOSSLESS_DREAM_MODEL` env var overrides that take precedence over config.json. Config file becomes optional overrides, not required setup.

**Rationale:** `embed.py:detect_provider()` already does exactly this pattern for embeddings — checking installed packages and env vars to pick a provider. The same approach should extend to `call_llm()`. Most users already have API keys set in their environment for Claude Code. Zero-config for the common case. Env vars are the natural configuration mechanism for hooks (which run in shell contexts).

**Downsides:** Detection priority order may surprise users (e.g., picks OpenAI when they expected Anthropic). Env var proliferation. Auto-detection adds a code path that's harder to test.

**Confidence:** 85%
**Complexity:** Low-Medium
**Status:** Explored (2026-04-07)

### 3. Provider Health Tracking and Error Visibility

**Description:** Replace the bare `except Exception: pass` in `call_llm()` with structured error handling. Distinguish auth errors (actionable: "API key invalid"), rate limits (retryable: back off), and network errors (transient: retry once). Log provider failures with timestamps to a lightweight tracking mechanism. Surface current provider health in `lcc status` output. When a provider has failed N times consecutively, emit a one-line warning into session context on SessionStart suggesting a fix. Optionally auto-fall through to the next available provider before resorting to extractive fallback.

**Rationale:** Today, if your API key expires or your provider goes down, `call_llm()` silently returns empty string and every summary degrades to extractive mode with zero indication. Users lose weeks of summarisation quality without knowing it. The project's own learnings doc explicitly flags bare `except Exception: pass` as forbidden. This is the most damaging UX issue in the entire system.

**Downsides:** Tracking state adds complexity to a currently stateless function. Needs careful design to avoid noisy warnings. Error classification differs across providers.

**Confidence:** 80%
**Complexity:** Low-Medium
**Status:** Explored (2026-04-07)

### 4. Structured Output for Cross-Model Dream Parsing

**Description:** Replace the fragile regex-based `_parse_pattern_response()` in `dream.py` (which parses `[CATEGORY] description (Source: ids)` from free text) with JSON mode / structured output where supported. The dream prompt would request JSON output with a defined schema (`{"patterns": [{"category": "...", "description": "...", "sources": [...]}]}`), and parsing becomes `json.loads()` instead of line-by-line string matching. Fall back to the current regex parser for providers that don't support JSON mode.

**Rationale:** Dream pattern extraction is the most cross-model-fragile code in the system. Different models format responses differently — extra whitespace, different bracket styles, preamble text before the patterns. The `_extractive_pattern_fallback()` exists specifically because LLM parsing is unreliable. JSON mode is supported by OpenAI, Anthropic, Ollama, and most OpenAI-compatible providers. This makes provider switching reliable for the most complex LLM feature.

**Downsides:** Not all providers support JSON mode equally well. Small/local models may produce invalid JSON. Requires maintaining two parsing paths (JSON + regex fallback). Prompt changes affect all existing dream output.

**Confidence:** 75%
**Complexity:** Medium
**Status:** Explored (2026-04-07)

### 5. Context Window Adaptation (Model-Aware Chunking)

**Description:** Build a lightweight model capability map (dict of model name -> context window size) for common models. Replace the fixed `chunkSize: 20` messages and `max_tokens: 2048` with model-aware defaults. Use `estimate_tokens()` (which already exists in `summarise.py:66-68` but is unused for chunking) to chunk by estimated token count instead of message count. Remove the 4000-char per-message truncation (`format_messages_for_summary` line 165) when the model can handle more. Allow `chunkSize` config to override for users who want manual control.

**Rationale:** Using a local 8K-context model (e.g., Llama3-8B) with 20-message chunks that exceed 8K tokens produces garbage or errors. Using a 200K-context model with the same conservative chunks wastes capability. The `format_messages_for_summary()` truncation at 4000 chars is a band-aid for when chunks contain long tool outputs — token-aware chunking would eliminate the need for it. Without this, switching providers silently produces bad results even when the LLM call succeeds.

**Downsides:** Model capability map needs periodic maintenance (new models). Token estimation is approximate. Adds config surface area. May change summary quality characteristics for existing users.

**Confidence:** 70%
**Complexity:** Medium
**Status:** Explored (2026-04-07)

### 6. Local-First Summarisation as First-Class Mode

**Description:** Elevate the extractive fallback from "degraded mode" to a proper `summaryProvider: "local"` option. Improve it beyond the current keyword-matching heuristic (which just looks for "decision" and "error" and falls back to first/last 5 lines) using TextRank or TF-IDF sentence scoring via existing stdlib capabilities. Optionally support local SLM inference via Ollama if available (detected automatically via Idea #2). Zero API keys, zero cost, works everywhere.

**Rationale:** The biggest friction for adoption is the API key requirement. Claude Max subscription users don't have API keys — they authenticate via OAuth tokens that only work with proxies. The extractive fallback at `summarise.py:137-155` already proves the system works without an LLM — it just needs to be better. The fastembed pattern (local ONNX models for embeddings) proves local ML works in this codebase.

**Downsides:** Even improved extractive summaries are noticeably worse than LLM summaries. Local SLMs (via Ollama) add significant dependency weight and require adequate hardware. Quality gap propagates through the DAG — depth-1 summaries built from poor depth-0 summaries compound the loss.

**Confidence:** 60%
**Complexity:** Medium-High
**Status:** Unexplored

### 7. Tiered Model Routing (Cheap for Summaries, Smart for Dreams)

**Description:** Formalize the existing `summaryModel`/`dreamModel` split into explicit tier-aware routing. Add a `handoffModel` config key. Allow depth-aware routing where depth-0 summaries use a fast/cheap model and higher-depth cascade summaries use a more capable model (since they compress already-compressed information and need more judgment). Default to the cheapest available model for bulk chunk summarisation and a more capable one for dream pattern extraction and DAG consolidation.

**Rationale:** `_dream_llm_cfg()` in `dream.py:46-51` already does manual key remapping from `dreamModel` to `summaryModel`, proving the tension exists. Dream pattern extraction requires genuine reasoning (categorizing, cross-referencing across sessions). Chunk summarisation is compression. Using Haiku for both is either overpaying for summaries or underpaying for dream quality. Quality at depth-0 propagates upward through the cascade, so the cost/quality tradeoff compounds.

**Downsides:** Adds config complexity — users must configure multiple models. Auto-detection (Idea #2) becomes harder with multiple model slots to fill. May confuse users who just want "one model for everything."

**Confidence:** 65%
**Complexity:** Low-Medium
**Status:** Explored (2026-04-07)

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Provider Registry / Pluggable Backends | Over-engineered for 2-3 providers; current if/elif works at this scale. Revisit when >5 providers exist |
| 2 | LiteLLM Integration | Adds heavy third-party dependency; OpenAI-compatible path achieves the same without coupling |
| 3 | Eliminate Summary DAG, Use Embeddings Only | Too radical; text summaries have clear value for context injection and human readability. Breaks core concept |
| 4 | Setup Wizard (`lcc setup`) | Low leverage; auto-detection (Idea #2) makes it unnecessary for the common case |
| 5 | Cost/Token Budget Controls | Premature for current user base; most users don't have LLM calls working yet |
| 6 | Adaptive Discovery with Probe Caching | Over-engineered version of auto-detect; probe-and-cache adds complexity without proportional benefit |
| 7 | Summary Quality Feedback Loop | Interesting but premature; no usage telemetry infrastructure exists |
| 8 | Prompt-Aware Summarisation | Too vague; "make summaries domain-aware" isn't actionable without specific improvements |
| 9 | Piggyback on Active Claude Session via MCP | Technically infeasible; MCP tools are invoked BY Claude, not the reverse |
| 10 | Unified Auth Resolution Layer | Subsumed by auto-detection (Idea #2) and OpenAI-compatible backend (Idea #1) |
| 11 | Config-Less via Env Vars Only | Merged into auto-detection (Idea #2) as env var overrides |
| 12 | Streaming with Progress Callbacks | Nice but irrelevant to the core problem; summaries happen in background |
| 13 | LLM Call Caching | Good idea but orthogonal to provider agnosticism; separate initiative |
| 14 | Embed Summaries Too | High value but orthogonal to provider support; separate initiative |
| 15 | Inherit Config from Claude Code Hook Payload | Depends on Claude Code exposing provider info in hooks, which it doesn't currently do |
| 16 | Adaptive Chunk Sizing by Token Count | Merged into Context Window Adaptation (Idea #5) |

## Session Log

- 2026-04-07: Initial ideation — 40 raw candidates from 5 parallel sub-agents (pain/friction, missing capability, inversion/automation, assumption-breaking, leverage/compounding), deduped to 23 unique, 7 survived adversarial filtering
- 2026-04-07: All 7 survivors explored via brainstorm -> requirements doc at `docs/brainstorms/2026-04-07-provider-agnostic-and-readme-requirements.md`
