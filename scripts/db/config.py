"""User-facing configuration for lossless-code."""

import json
import os

DEFAULT_CONFIG = {
    "summaryModel": "claude-haiku-4-5-20251001",
    "summaryProvider": None,  # None = auto-detect from environment
    "anthropicBaseUrl": None,
    "openaiBaseUrl": None,
    "chunkSize": 20,
    "depthThreshold": 10,
    "incrementalMaxDepth": 5,
    "workingDirFilter": None,
    # Summary size caps (prevent vault bloat)
    "leafTargetTokens": 2400,
    "condensedTargetTokens": 2000,
    "summaryMaxOverageFactor": 3,
    # Dream cycle
    "autoDream": True,
    "dreamAfterSessions": 5,
    "dreamAfterHours": 24,
    "dreamModel": "claude-haiku-4-5-20251001",
    "handoffModel": None,  # Falls back to summaryModel
    "dreamTokenBudget": 2000,
    "dreamBatchSize": 100,
    # Session filtering (lossless-claw parity)
    "ignoreSessionPatterns": [],      # sessions matching these patterns are never stored
    "statelessSessionPatterns": [],   # sessions matching these patterns skip summarization
    # Summarization reliability (circuit breaker)
    "circuitBreakerEnabled": True,    # stop calling LLM after N consecutive failures
    "circuitBreakerThreshold": 5,     # number of failures before breaker trips
    "circuitBreakerCooldownMs": 1800000,  # 30 min: time before breaker auto-resets
    # Dynamic chunk sizing (lossless-claw parity)
    "dynamicChunkSize": {
        "enabled": True,
        "max": 50,  # maximum chunk size for busy sessions; chunkSize is the floor
    },
    # Semantic search (Phase 2)
    "embeddingEnabled": False,
    "embeddingProvider": "local",
    "embeddingModel": "BAAI/bge-small-en-v1.5",
    "ftsWeight": 1.0,
    "vectorWeight": 1.0,
    "lastEmbeddingModel": None,
    "vectorBackend": "auto",
    # Fingerprint file context (v1.3.0, default off)
    "fileContextEnabled": False,
    # v1.2 compaction-aware bundle (U10 + U6)
    "bundleEnabled": True,
    "bundleTokenBudget": 1000,
    "taskStateEnabled": True,
    "taskStateTokenBudget": 200,
    "codexTailImportProjectRoots": [],
    "codexTailImportMaxFiles": 200,
    "codexTailImportMaxTailLines": 120,
    "codexTailImportMaxTailBytes": 120000,
    "codexTailImportTimeoutMs": 1500,
    "codexTailImportCodexHome": None,
    "contractsModel": None,  # Falls back to dreamModel then summaryModel
    "contractsPerCycleLimit": 10,
    "decisionsPerCycleLimit": 15,
}


def load_config() -> dict:
    from . import CONFIG_PATH
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user_cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **user_cfg}
    else:
        merged = dict(DEFAULT_CONFIG)
    # Env var overrides (highest priority)
    for env_key, cfg_key in [
        ("LOSSLESS_SUMMARY_PROVIDER", "summaryProvider"),
        ("LOSSLESS_SUMMARY_MODEL", "summaryModel"),
        ("LOSSLESS_DREAM_MODEL", "dreamModel"),
    ]:
        val = os.environ.get(env_key)
        if val:
            merged[cfg_key] = val
    return merged


def save_config(cfg: dict) -> None:
    from . import CONFIG_PATH, VAULT_DIR
    VAULT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(VAULT_DIR, 0o700)
    except OSError:
        pass  # Best-effort on shared/readonly mounts
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    # Lock down config.json. May contain provider config, base URLs, and model
    # selections that should not be world-readable on shared machines.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


__all__ = ["DEFAULT_CONFIG", "load_config", "save_config"]
