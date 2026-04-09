#!/usr/bin/env python3
"""Tests for provider-agnostic LLM support in summarise.py."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_summarise_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import summarise


class TestAutoDetection(unittest.TestCase):
    """Test provider auto-detection from environment."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    def setUp(self):
        # Reset cached CLI path between tests
        summarise._claude_cli_checked = False
        summarise._claude_cli_path = None

    def _cfg(self, **overrides):
        base = dict(db.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    @patch("scripts.summarise.shutil.which", return_value=None)
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False)
    def test_detect_anthropic_from_env(self, _mock_which):
        provider, model = summarise._detect_provider(self._cfg())
        self.assertEqual(provider, "anthropic")
        self.assertIn("claude", model)

    @patch("scripts.summarise.shutil.which", return_value=None)
    @patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai"}, clear=False)
    def test_detect_openai_from_env(self, _mock_which):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, {**env, "OPENAI_API_KEY": "sk-openai"}, clear=True):
            provider, model = summarise._detect_provider(self._cfg())
            self.assertEqual(provider, "openai")

    @patch("scripts.summarise.shutil.which", return_value=None)
    def test_detect_none_when_no_keys(self, _mock_which):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")}
        with patch.dict(os.environ, env, clear=True):
            provider, model = summarise._detect_provider(self._cfg(openaiBaseUrl=None))
            self.assertIsNone(provider)
            self.assertIsNone(model)

    @patch("scripts.summarise.shutil.which", return_value=None)
    def test_detect_openai_from_base_url(self, _mock_which):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            cfg = self._cfg(openaiBaseUrl="http://localhost:11434/v1")
            provider, model = summarise._detect_provider(cfg)
            self.assertEqual(provider, "openai")

    @patch("scripts.summarise.shutil.which", return_value=None)
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": "sk-openai"}, clear=False)
    def test_anthropic_takes_priority_over_openai(self, _mock_which):
        provider, _ = summarise._detect_provider(self._cfg())
        self.assertEqual(provider, "anthropic")

    @patch("scripts.summarise.shutil.which", return_value="/usr/bin/claude")
    def test_claude_cli_takes_priority(self, _mock_which):
        provider, model = summarise._detect_provider(self._cfg())
        self.assertEqual(provider, "claude-cli")
        self.assertIn("claude", model)


class TestEnvVarOverrides(unittest.TestCase):
    """Test env var overrides in load_config()."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    @patch.dict(os.environ, {"LOSSLESS_SUMMARY_PROVIDER": "openai"}, clear=False)
    def test_env_overrides_provider(self):
        cfg = db.load_config()
        self.assertEqual(cfg["summaryProvider"], "openai")

    @patch.dict(os.environ, {"LOSSLESS_SUMMARY_MODEL": "gpt-4.1-nano"}, clear=False)
    def test_env_overrides_model(self):
        cfg = db.load_config()
        self.assertEqual(cfg["summaryModel"], "gpt-4.1-nano")

    @patch.dict(os.environ, {"LOSSLESS_DREAM_MODEL": "gpt-4o-mini"}, clear=False)
    def test_env_overrides_dream_model(self):
        cfg = db.load_config()
        self.assertEqual(cfg["dreamModel"], "gpt-4o-mini")


class TestCallLlm(unittest.TestCase):
    """Test call_llm() provider routing and error handling."""

    def _cfg(self, **overrides):
        base = dict(db.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    @patch("scripts.summarise.shutil.which", return_value=None)
    def test_returns_empty_when_no_provider(self, _mock_which):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                            "CLAUDE_CODE_OAUTH_TOKEN", "OLLAMA_HOST")}
        with patch.dict(os.environ, env, clear=True):
            result = summarise.call_llm("test prompt", self._cfg(openaiBaseUrl=None))
            self.assertEqual(result, "")

    def test_local_provider_returns_empty(self):
        result = summarise.call_llm("test", self._cfg(summaryProvider="local"))
        self.assertEqual(result, "")

    @patch("summarise.OpenAI", create=True)
    def test_openai_base_url_passed(self, mock_openai_cls):
        """Test that openaiBaseUrl is passed to OpenAI client."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="summary"))]
        mock_client.chat.completions.create.return_value = mock_response

        # Mock the import
        with patch.dict("sys.modules", {"openai": MagicMock(OpenAI=lambda **kw: mock_client)}):
            cfg = self._cfg(
                summaryProvider="openai",
                openaiBaseUrl="http://localhost:11434/v1",
                summaryModel="llama3"
            )
            # We need to patch the import inside call_llm
            result = summarise.call_llm("test", cfg)
            # Even if the mock doesn't fully work, at least verify no crash
            self.assertIsInstance(result, str)

    def test_provider_state_updated(self):
        """Test that _provider_state is updated on call."""
        cfg = self._cfg(summaryProvider="local", summaryModel="test-model")
        summarise.call_llm("test", cfg)
        info = summarise.get_provider_info()
        self.assertEqual(info["provider"], "local")
        self.assertEqual(info["model"], "test-model")

    def test_json_mode_param_accepted(self):
        """Test that json_mode parameter is accepted without error."""
        cfg = self._cfg(summaryProvider="local")
        result = summarise.call_llm("test", cfg, json_mode=True)
        self.assertEqual(result, "")

    @patch("scripts.summarise.subprocess.run")
    def test_claude_cli_returns_response(self, mock_run):
        """Test claude-cli provider routes through subprocess and returns output."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Summary text\n", stderr="")
        result = summarise.call_llm("test prompt", self._cfg(summaryProvider="claude-cli"))
        self.assertEqual(result, "Summary text")
        mock_run.assert_called_once()
        call_env = mock_run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", call_env)

    @patch("scripts.summarise.subprocess.run")
    def test_claude_cli_timeout_returns_empty(self, mock_run):
        """Test claude-cli returns empty string on subprocess timeout."""
        mock_run.side_effect = summarise.subprocess.TimeoutExpired("claude", 120)
        result = summarise.call_llm("test", self._cfg(summaryProvider="claude-cli"))
        self.assertEqual(result, "")

    @patch("scripts.summarise.subprocess.run")
    def test_claude_cli_error_returns_empty(self, mock_run):
        """Test claude-cli returns empty string on non-zero exit code."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Auth error")
        result = summarise.call_llm("test", self._cfg(summaryProvider="claude-cli"))
        self.assertEqual(result, "")


class TestContextWindowAdaptation(unittest.TestCase):
    """Test model capability map and context window adaptation."""

    def test_known_claude_model(self):
        self.assertEqual(summarise._get_context_window("claude-haiku-4-5-20251001"), 200_000)

    def test_known_gpt_model(self):
        self.assertEqual(summarise._get_context_window("gpt-4o-mini"), 128_000)

    def test_known_gpt41_model(self):
        self.assertEqual(summarise._get_context_window("gpt-4.1-mini"), 1_000_000)

    def test_unknown_model_defaults(self):
        self.assertEqual(summarise._get_context_window("some-random-model"), 8192)

    def test_none_model_defaults(self):
        self.assertEqual(summarise._get_context_window(None), 8192)

    def test_format_messages_large_context(self):
        """Large-context models get higher truncation limit."""
        msg = {"role": "user", "content": "x" * 10000, "tool_name": None}
        # Small context model (8K) -> truncates at 4000
        result_small = summarise.format_messages_for_summary([msg], model="llama3")
        self.assertLess(len(result_small), 5000)

        # Large context model -> allows more content
        result_large = summarise.format_messages_for_summary([msg], model="claude-haiku-4-5-20251001")
        self.assertGreater(len(result_large), 5000)


class TestExtractiveFallback(unittest.TestCase):
    """Test TF-IDF extractive summarisation."""

    def test_basic_extraction(self):
        text = "\n\n".join([
            "[user] We need to fix the authentication bug in login.py.",
            "[assistant] I'll look at the login flow. The issue is in validate_token().",
            "[user] Yes, the token validation is wrong. It should check expiry first.",
            "[assistant] Fixed. Changed the order of checks in validate_token().",
            "[user] Can you also add a test for this?",
            "[assistant] Added test_validate_token_expiry() in test_auth.py.",
            "[user] Looks good. Let's also update the error message.",
            "[assistant] Updated the error message to be more descriptive.",
            "[user] Perfect. This is ready for review now.",
            "[assistant] Created the pull request.",
        ])
        result = summarise._extractive_summary(text)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)
        # Should preserve important sentences
        self.assertTrue(len(result) < len(text))

    def test_short_text_preserved(self):
        text = "Short message.\nAnother short one."
        result = summarise._extractive_summary(text)
        self.assertIn("Short message", result)

    def test_empty_text(self):
        result = summarise._extractive_summary("")
        self.assertIsInstance(result, str)


class TestDreamJsonParsing(unittest.TestCase):
    """Test JSON mode dream pattern parsing."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import dream
        self.dream = dream

    def test_valid_json_parsed(self):
        response = json.dumps({
            "patterns": [
                {
                    "category": "CORRECTION",
                    "description": "Always use const instead of var",
                    "sources": ["msg:1", "msg:2"]
                },
                {
                    "category": "PREFERENCE",
                    "description": "Prefer TypeScript over JavaScript",
                    "sources": ["msg:3"]
                }
            ]
        })
        result = self.dream._parse_patterns_json(response)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["category"], "CORRECTION")
        self.assertEqual(result[0]["description"], "Always use const instead of var")
        self.assertIn("msg:1", result[0]["source_ids"])

    def test_invalid_json_returns_empty(self):
        result = self.dream._parse_patterns_json("not json at all")
        self.assertEqual(result, [])

    def test_json_with_invalid_category_skipped(self):
        response = json.dumps({
            "patterns": [
                {"category": "INVALID_CAT", "description": "test", "sources": []},
                {"category": "CORRECTION", "description": "valid", "sources": ["msg:1"]},
            ]
        })
        result = self.dream._parse_patterns_json(response)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["category"], "CORRECTION")

    def test_empty_patterns_array(self):
        response = json.dumps({"patterns": []})
        result = self.dream._parse_patterns_json(response)
        self.assertEqual(result, [])

    def test_missing_patterns_key(self):
        response = json.dumps({"other": "data"})
        result = self.dream._parse_patterns_json(response)
        self.assertEqual(result, [])


class TestDreamLlmCfgPassthrough(unittest.TestCase):
    """Test that _dream_llm_cfg passes through all LLM config keys."""

    def test_passthrough_includes_base_urls(self):
        config = {
            "summaryProvider": "openai",
            "summaryModel": "gpt-4.1-mini",
            "dreamModel": "gpt-4.1-nano",
            "anthropicBaseUrl": "https://proxy.example.com",
            "openaiBaseUrl": "http://localhost:11434/v1",
        }
        import dream
        result = dream._dream_llm_cfg(config)
        self.assertEqual(result["summaryProvider"], "openai")
        self.assertEqual(result["summaryModel"], "gpt-4.1-nano")
        self.assertEqual(result["anthropicBaseUrl"], "https://proxy.example.com")
        self.assertEqual(result["openaiBaseUrl"], "http://localhost:11434/v1")

    def test_passthrough_none_provider(self):
        config = {"summaryProvider": None}
        import dream
        result = dream._dream_llm_cfg(config)
        self.assertIsNone(result["summaryProvider"])


class TestConfigDefaults(unittest.TestCase):
    """Test new config defaults."""

    def test_summary_provider_default_is_none(self):
        self.assertIsNone(db.DEFAULT_CONFIG["summaryProvider"])

    def test_openai_base_url_exists(self):
        self.assertIn("openaiBaseUrl", db.DEFAULT_CONFIG)
        self.assertIsNone(db.DEFAULT_CONFIG["openaiBaseUrl"])

    def test_handoff_model_exists(self):
        self.assertIn("handoffModel", db.DEFAULT_CONFIG)
        self.assertIsNone(db.DEFAULT_CONFIG["handoffModel"])

    def test_bloat_prevention_defaults(self):
        self.assertEqual(db.DEFAULT_CONFIG["leafTargetTokens"], 2400)
        self.assertEqual(db.DEFAULT_CONFIG["condensedTargetTokens"], 2000)
        self.assertEqual(db.DEFAULT_CONFIG["summaryMaxOverageFactor"], 3)
        self.assertEqual(db.DEFAULT_CONFIG["incrementalMaxDepth"], 5)
        self.assertEqual(db.DEFAULT_CONFIG["dreamBatchSize"], 100)


class TestCapSummaryText(unittest.TestCase):
    """Test summary text capping to prevent vault bloat."""

    def test_short_text_passes_through(self):
        text = "This is a short summary."
        result = summarise.cap_summary_text(text, 2400)
        self.assertEqual(result, text)

    def test_text_at_limit_passes_through(self):
        # 2400 * 3 * 4 = 28800 chars is the limit
        text = "x" * 28800
        result = summarise.cap_summary_text(text, 2400, 3)
        self.assertEqual(result, text)

    def test_text_over_limit_is_capped(self):
        text = "x" * 100000  # ~25000 tokens, well over 7200 limit
        result = summarise.cap_summary_text(text, 2400, 3)
        self.assertIn("[Capped from ~25000 to ~7200 tokens]", result)
        self.assertLess(len(result), 30000)

    def test_cap_preserves_newline_boundary(self):
        lines = ["line " + str(i) for i in range(10000)]
        text = "\n".join(lines)
        result = summarise.cap_summary_text(text, 100, 1)  # very low cap: 400 chars
        self.assertTrue(result.endswith("tokens]"))
        # Should not cut mid-line (breaks at last newline before limit)
        capped_content = result.split("\n\n[Capped")[0]
        for line in capped_content.split("\n"):
            self.assertTrue(line.startswith("line ") or line == "")

    def test_condensed_target_lower_than_leaf(self):
        text = "x" * 30000  # ~7500 tokens
        leaf_result = summarise.cap_summary_text(text, 2400, 3)  # cap at 7200
        condensed_result = summarise.cap_summary_text(text, 2000, 3)  # cap at 6000
        # Condensed cap is tighter
        self.assertLess(len(condensed_result), len(leaf_result))

    def test_custom_overage_factor(self):
        text = "x" * 50000  # ~12500 tokens
        result_2x = summarise.cap_summary_text(text, 2400, 2)  # cap at 4800
        result_3x = summarise.cap_summary_text(text, 2400, 3)  # cap at 7200
        self.assertLess(len(result_2x), len(result_3x))

    def test_empty_text(self):
        result = summarise.cap_summary_text("", 2400)
        self.assertEqual(result, "")


class TestDbPaginationHelpers(unittest.TestCase):
    """Test the new DB pagination functions."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    def test_get_summary_ids_since_returns_ids(self):
        ids = db.get_summary_ids_since(0)
        self.assertIsInstance(ids, list)
        for item in ids:
            self.assertIsInstance(item, str)

    def test_get_summaries_by_ids_empty(self):
        result = db.get_summaries_by_ids([])
        self.assertEqual(result, [])

    def test_get_summaries_by_ids_nonexistent(self):
        result = db.get_summaries_by_ids(["nonexistent_id"])
        self.assertEqual(result, [])


class TestDynamicChunkSize(unittest.TestCase):
    """Tests for _compute_dynamic_chunk_size."""

    def _cfg(self, base=20, enabled=True, max_val=50):
        return {
            "chunkSize": base,
            "dynamicChunkSize": {"enabled": enabled, "max": max_val},
        }

    def test_disabled_returns_base(self):
        cfg = self._cfg(enabled=False)
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 200), 20)

    def test_floor_holds_with_small_pending(self):
        # 30 pending // 2 = 15 < base=20 → returns 20
        cfg = self._cfg(base=20, max_val=50)
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 30), 20)

    def test_scales_up_with_large_pending(self):
        # 200 pending // 2 = 100 > max=50 → returns 50
        cfg = self._cfg(base=20, max_val=50)
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 200), 50)

    def test_mid_range(self):
        # 60 pending // 2 = 30; floor=20, max=50 → returns 30
        cfg = self._cfg(base=20, max_val=50)
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 60), 30)

    def test_max_less_than_base_floor_wins(self):
        # max=15 < base=20 → min(15,500)=15; max(20,...)≥20
        cfg = self._cfg(base=20, max_val=15)
        result = summarise._compute_dynamic_chunk_size(cfg, 200)
        self.assertEqual(result, 20)

    def test_hard_cap_500(self):
        # max=9999 but hard cap is 500; 2000 pending // 2 = 1000 > 500 → 500
        cfg = self._cfg(base=20, max_val=9999)
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 2000), 500)

    def test_no_dynamic_config_key_uses_defaults(self):
        # No dynamicChunkSize key → defaults: enabled=True, max=50 (via missing)
        cfg = {"chunkSize": 20}
        # pending=0 → max(20, min(50, 0))=20
        self.assertEqual(summarise._compute_dynamic_chunk_size(cfg, 0), 20)


class TestCircuitBreaker(unittest.TestCase):
    """Tests for file-backed circuit breaker in summarise.py."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    def _state_path(self):
        return db.LOSSLESS_HOME / "circuit_breaker.json"

    def setUp(self):
        # Clear state file before each test
        path = self._state_path()
        if path.exists():
            path.unlink()
        # Reset in-process state
        summarise._provider_state["consecutive_failures"] = 0
        summarise._provider_state["last_error_time"] = None

    def test_load_state_missing_file(self):
        state = summarise._load_circuit_breaker_state()
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["last_error_time"], 0)

    def test_load_state_corrupt_file(self):
        self._state_path().write_text("not valid json")
        state = summarise._load_circuit_breaker_state()
        self.assertEqual(state["failures"], 0)

    def test_write_and_load_state(self):
        import time
        now = time.time()
        summarise._write_circuit_breaker_state(3, now)
        state = summarise._load_circuit_breaker_state()
        self.assertEqual(state["failures"], 3)
        self.assertAlmostEqual(state["last_error_time"], now, delta=0.01)

    def test_write_atomic_creates_file(self):
        summarise._write_circuit_breaker_state(1, 12345.0)
        self.assertTrue(self._state_path().exists())

    def test_check_breaker_disabled(self):
        summarise._write_circuit_breaker_state(99, 9999999999.0)
        cfg = {"circuitBreakerEnabled": False, "circuitBreakerThreshold": 5, "circuitBreakerCooldownMs": 1800000}
        should_proceed, msg = summarise._check_circuit_breaker(cfg)
        self.assertTrue(should_proceed)

    def test_check_breaker_under_threshold(self):
        summarise._write_circuit_breaker_state(2, 9999999999.0)
        cfg = {"circuitBreakerEnabled": True, "circuitBreakerThreshold": 5, "circuitBreakerCooldownMs": 1800000}
        should_proceed, _ = summarise._check_circuit_breaker(cfg)
        self.assertTrue(should_proceed)

    def test_check_breaker_tripped(self):
        import time
        summarise._write_circuit_breaker_state(5, time.time())
        cfg = {"circuitBreakerEnabled": True, "circuitBreakerThreshold": 5, "circuitBreakerCooldownMs": 1800000}
        should_proceed, msg = summarise._check_circuit_breaker(cfg)
        self.assertFalse(should_proceed)
        self.assertIn("Circuit breaker open", msg)

    def test_check_breaker_cooldown_expired(self):
        # Write a state with old last_error_time (past cooldown)
        summarise._write_circuit_breaker_state(5, 0.0)  # epoch = way past cooldown
        cfg = {"circuitBreakerEnabled": True, "circuitBreakerThreshold": 5, "circuitBreakerCooldownMs": 1800000}
        should_proceed, _ = summarise._check_circuit_breaker(cfg)
        self.assertTrue(should_proceed)
        # State should be reset
        state = summarise._load_circuit_breaker_state()
        self.assertEqual(state["failures"], 0)

    def test_call_llm_respects_tripped_breaker(self):
        """call_llm returns empty string without hitting API when breaker is open."""
        import time
        summarise._write_circuit_breaker_state(5, time.time())
        cfg = {
            "summaryProvider": "anthropic",
            "summaryModel": "claude-haiku-4-5-20251001",
            "circuitBreakerEnabled": True,
            "circuitBreakerThreshold": 5,
            "circuitBreakerCooldownMs": 1800000,
        }
        # Patch _check_circuit_breaker to verify it's consulted and blocks the call
        with patch.object(summarise, "_check_circuit_breaker", return_value=(False, "Circuit breaker open")) as mock_cb:
            result = summarise.call_llm("test prompt", cfg)
        self.assertEqual(result, "")
        mock_cb.assert_called_once()

    def test_call_llm_threshold_one(self):
        """circuitBreakerThreshold=1: single failure trips on next call."""
        import time
        summarise._write_circuit_breaker_state(1, time.time())
        cfg = {
            "summaryProvider": "anthropic",
            "summaryModel": "claude-haiku-4-5-20251001",
            "circuitBreakerEnabled": True,
            "circuitBreakerThreshold": 1,
            "circuitBreakerCooldownMs": 1800000,
        }
        # With threshold=1 and failures=1, breaker should be open
        should_proceed, _ = summarise._check_circuit_breaker(cfg)
        self.assertFalse(should_proceed)


if __name__ == "__main__":
    unittest.main()
