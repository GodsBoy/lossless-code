---
status: pending
priority: p2
issue_id: "009"
tags: [code-review, security]
dependencies: []
---

# vault.db and config.json Created World-Readable (644 permissions)

## Problem Statement

`vault.db` contains the full conversation history of every Claude Code session — code, credentials typed into prompts, file contents pasted for review, any secrets that appeared in transcript. `config.json` may contain API provider configuration. Both are created with default `644` permissions (world-readable). On any multi-user system or machine with a compromised user account, any local process can read the entire vault.

## Findings

**Agent:** security-sentinel (P2)

- `scripts/db.py:150` — `VAULT_DIR.mkdir(parents=True, exist_ok=True)` — no mode specified
- `scripts/db.py:238` — `open(CONFIG_PATH, "w")` — no mode specified
- Current permissions confirmed: `644 vault.db`, `644 config.json`

## Proposed Solutions

### Option A — Restrict directory and file permissions on creation (Recommended)

```python
# db.py — vault directory
VAULT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

# db.py — config file write
import stat
fd = os.open(str(CONFIG_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
```

For the SQLite database (no `mode` param in `sqlite3.connect`):
```python
# After first creation
if not VAULT_DB.exists():
    # ... connect
    os.chmod(str(VAULT_DB), 0o600)
```

- Pros: Vault content is protected at rest; standard Unix security hygiene
- Cons: Existing installations keep old permissions — needs one-time migration or chmod on open
- Effort: Small
- Risk: Low — chmod only restricts other users, not the owner

### Option B — Add a one-time migration in `get_db()`

On every startup, `chmod 600 vault.db` and `chmod 600 config.json` if they exist with looser permissions.

- Pros: Fixes existing installations automatically
- Cons: Runs a syscall on every db open
- Effort: Small

## Recommended Action

_Option A for new installations + Option B migration applied once in `get_db()` startup path._

## Technical Details

**Affected files:** `scripts/db.py`

## Acceptance Criteria

- [ ] New `~/.lossless-code/` directories created with `mode=0o700`
- [ ] New `vault.db` created with permissions `600`
- [ ] New `config.json` written with permissions `600`
- [ ] Existing installations have permissions tightened on next startup

## Work Log

- 2026-03-29 — Identified by security-sentinel
