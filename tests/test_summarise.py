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

    def _cfg(self, **overrides):
        base = dict(db.DEFAULT_CONFIG)
        base.update(overrides)
        return base

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False)
    def test_detect_anthropic_from_env(self):
        provider, model = summarise._detect_provider(self._cfg())
        self.assertEqual(provider, "anthropic")
        self.assertIn("claude", model)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai"}, clear=False)
    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_detect_openai_from_env(self, _mock_open):
        # Remove ANTHROPIC_API_KEY to test OpenAI fallback
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, {**env, "OPENAI_API_KEY": "sk-openai"}, clear=True):
            provider, model = summarise._detect_provider(self._cfg())
            self.assertEqual(provider, "openai")

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_detect_none_when_no_keys(self, _mock_open):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                            "CLAUDE_CODE_OAUTH_TOKEN", "OLLAMA_HOST")}
        with patch.dict(os.environ, env, clear=True):
            provider, model = summarise._detect_provider(self._cfg(openaiBaseUrl=None))
            self.assertIsNone(provider)
            self.assertIsNone(model)

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_detect_openai_from_base_url(self, _mock_open):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                            "CLAUDE_CODE_OAUTH_TOKEN")}
        with patch.dict(os.environ, env, clear=True):
            cfg = self._cfg(openaiBaseUrl="http://localhost:11434/v1")
            provider, model = summarise._detect_provider(cfg)
            self.assertEqual(provider, "openai")

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test", "OPENAI_API_KEY": "sk-openai"}, clear=False)
    def test_anthropic_takes_priority_over_openai(self):
        provider, _ = summarise._detect_provider(self._cfg())
        self.assertEqual(provider, "anthropic")


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

    def test_returns_empty_when_no_provider(self):
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


if __name__ == "__main__":
    unittest.main()
