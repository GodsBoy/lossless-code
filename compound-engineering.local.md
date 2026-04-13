---
review_agents:
  - compound-engineering:review:kieran-python-reviewer
  - compound-engineering:review:security-sentinel
  - compound-engineering:review:performance-oracle
  - compound-engineering:review:architecture-strategist
  - compound-engineering:review:code-simplicity-reviewer
---

## Project Context

lossless-code is a Python CLI plugin for Claude Code that captures conversation memory into a local SQLite vault with FTS5 search. It runs as hooks (SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompact) and exposes an MCP server.

Key constraints for reviewers:
- All SQL must live in the `scripts/db/` package only — no raw queries in other files.
- `scripts/embed.py` is the search orchestration layer — it imports db via deferred `import db as _db` to avoid circular imports.
- Every vector/embedding code path must be behind try/import guards — the plugin must work with zero extra dependencies.
- File size hard max: 800 lines per file.
- Background processes must be non-blocking (nohup pattern).
- No external API calls without explicit user config (`embeddingEnabled: true`).
