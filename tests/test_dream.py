#!/usr/bin/env python3
"""Tests for lossless-code dream engine."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_dream_engine_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import dream


class TestDreamEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        dream.DREAM_DIR = db.VAULT_DIR / "dream"
        dream.DREAM_DIR.mkdir(parents=True, exist_ok=True)
        dream.LOG_FILE = dream.DREAM_DIR / "dream.log"
        db.get_db()

        # Seed test data
        db.ensure_session("dream-test-1", "/tmp/dreamproject")
        db.store_message("dream-test-1", "user", "Don't use var, always use const", working_dir="/tmp/dreamproject")
        db.store_message("dream-test-1", "assistant", "Understood, switching to const", working_dir="/tmp/dreamproject")
        db.store_message("dream-test-1", "user", "Prefer TypeScript over JavaScript", working_dir="/tmp/dreamproject")
        db.store_message("dream-test-1", "assistant", "Noted, using TypeScript going forward", working_dir="/tmp/dreamproject")
        db.store_message("dream-test-1", "user", "No, wrong approach, should be using hooks", working_dir="/tmp/dreamproject")
        db.store_message("dream-test-1", "assistant", "Switching to hooks pattern", working_dir="/tmp/dreamproject")

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_parse_pattern_response(self):
        response = (
            "[CORRECTION] Use const instead of var. (Source: msg:1, msg:2)\n"
            "[PREFERENCE] Prefer TypeScript over JavaScript. (Source: msg:3)\n"
            "[CONVENTION] Use hooks pattern for state management. (Source: sum_abc123)\n"
        )
        patterns = dream._parse_pattern_response(response)
        self.assertEqual(len(patterns), 3)
        self.assertEqual(patterns[0]["category"], "CORRECTION")
        self.assertIn("const", patterns[0]["description"])
        self.assertIn("msg:1", patterns[0]["source_ids"])
        self.assertEqual(patterns[1]["category"], "PREFERENCE")
        self.assertEqual(patterns[2]["category"], "CONVENTION")

    def test_parse_pattern_response_empty(self):
        patterns = dream._parse_pattern_response("")
        self.assertEqual(patterns, [])

    def test_parse_pattern_response_no_source(self):
        response = "[DECISION] We chose React for the frontend.\n"
        patterns = dream._parse_pattern_response(response)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["category"], "DECISION")
        self.assertEqual(patterns[0]["source_ids"], "")

    def test_extractive_fallback(self):
        messages = [
            {"id": 1, "role": "user", "content": "Don't use var, use const instead."},
            {"id": 2, "role": "user", "content": "Always prefer hooks over classes."},
            {"id": 3, "role": "assistant", "content": "OK, switching to hooks."},
        ]
        patterns = dream._extractive_pattern_fallback(messages)
        self.assertGreater(len(patterns), 0)
        categories = [p["category"] for p in patterns]
        self.assertTrue(
            any(c in categories for c in ["CORRECTION", "PREFERENCE"]),
            f"Expected CORRECTION or PREFERENCE, got {categories}",
        )

    def test_extractive_fallback_empty(self):
        patterns = dream._extractive_pattern_fallback([])
        self.assertEqual(patterns, [])

    @patch("dream.summarise_mod.call_llm")
    def test_extract_patterns_with_mock_llm(self, mock_llm):
        mock_llm.return_value = (
            "[CORRECTION] Use const instead of var. (Source: msg:1)\n"
            "[PREFERENCE] TypeScript preferred. (Source: msg:3)\n"
        )
        messages = [
            {"id": 1, "role": "user", "content": "Use const not var"},
            {"id": 3, "role": "user", "content": "Use TypeScript"},
        ]
        config = db.load_config()
        patterns = dream.extract_patterns(messages, [], config)
        self.assertEqual(len(patterns), 2)
        self.assertEqual(patterns[0]["category"], "CORRECTION")

    def test_write_patterns(self):
        patterns = [
            {"category": "CORRECTION", "description": "Use const", "source_ids": "msg:1", },
            {"category": "PREFERENCE", "description": "Use TS", "source_ids": "msg:2", },
        ]
        phash = db.project_hash("/tmp/dreamproject")
        path = dream.write_patterns(patterns, phash, "/tmp/dreamproject", "project")
        self.assertTrue(os.path.exists(path))

        content = open(path).read()
        self.assertIn("Dream Patterns", content)
        self.assertIn("Corrections", content)
        self.assertIn("Use const", content)
        self.assertIn("msg:1", content)
        self.assertIn("Preferences", content)

    def test_write_patterns_global(self):
        patterns = [
            {"category": "CONVENTION", "description": "Use conventional commits", "source_ids": "", },
        ]
        path = dream.write_patterns(patterns, "global", "", "global")
        self.assertTrue(os.path.exists(path))
        content = open(path).read()
        self.assertIn("Conventions", content)

    def test_generate_report(self):
        patterns = [
            {"category": "CORRECTION", "description": "test", "source_ids": "", },
            {"category": "PREFERENCE", "description": "test2", "source_ids": "", },
        ]
        consolidation_stats = {0: {"consolidated": 3}}
        path = dream.generate_report(
            patterns, consolidation_stats, "project", "/tmp/dreamproject", 5, 12.3,
        )
        self.assertTrue(os.path.exists(path))
        content = open(path).read()
        self.assertIn("Dream Report", content)
        self.assertIn("**Total:** 2", content)
        self.assertIn("12.3s", content)
        self.assertIn("5", content)

    def test_check_auto_trigger_disabled(self):
        config = {**db.load_config(), "autoDream": False}
        self.assertFalse(dream.check_auto_trigger(config, "/tmp/dreamproject"))

    def test_check_auto_trigger_no_sessions(self):
        config = {**db.load_config(), "autoDream": True, "dreamAfterSessions": 100}
        # With a very high threshold and our few test sessions, should be false
        self.assertFalse(dream.check_auto_trigger(config, "/tmp/no-such-dir-xyz"))

    def test_check_auto_trigger_after_sessions(self):
        config = {**db.load_config(), "autoDream": True, "dreamAfterSessions": 1}
        # We have at least 1 session for /tmp/dreamproject
        self.assertTrue(dream.check_auto_trigger(config, "/tmp/dreamproject"))

    def test_cluster_overlapping(self):
        pairs = [("a", "b"), ("b", "c"), ("d", "e")]
        clusters = dream._cluster_overlapping(pairs)
        # a,b,c should be one cluster; d,e another
        self.assertEqual(len(clusters), 2)
        cluster_sets = [frozenset(c) for c in clusters]
        self.assertIn(frozenset({"a", "b", "c"}), cluster_sets)
        self.assertIn(frozenset({"d", "e"}), cluster_sets)

    def test_dedup_merge(self):
        texts = [
            "Line one\nLine two\nLine three",
            "Line two\nLine four",
        ]
        result = dream._dedup_merge(texts)
        self.assertIn("Line one", result)
        self.assertIn("Line two", result)
        self.assertIn("Line three", result)
        self.assertIn("Line four", result)
        # Line two should appear only once
        self.assertEqual(result.count("Line two"), 1)

    @patch("dream.summarise_mod.call_llm")
    @patch("dream.summarise_mod.run_full_summarisation")
    def test_run_dream_empty_vault(self, mock_summarise, mock_llm):
        mock_summarise.return_value = {"depth_0_created": 0, "cascaded_created": 0}
        # Use a dir with no sessions
        config = db.load_config()
        result = dream.run_dream("project", "/tmp/empty-vault-xyz", config)
        self.assertIn("Nothing to dream about", result)

    @patch("dream.summarise_mod.call_llm")
    @patch("dream.summarise_mod.run_full_summarisation")
    def test_run_dream_full_cycle(self, mock_summarise, mock_llm):
        mock_summarise.return_value = {"depth_0_created": 0, "cascaded_created": 0}
        mock_llm.return_value = (
            "[CORRECTION] Use const. (Source: msg:1)\n"
            "[PREFERENCE] TypeScript preferred. (Source: msg:3)\n"
        )
        config = db.load_config()
        result = dream.run_dream("project", "/tmp/dreamproject", config)
        self.assertIn("Dream complete", result)
        self.assertIn("Patterns:", result)
        self.assertIn("Report:", result)

    @patch("dream.summarise_mod.call_llm")
    @patch("dream.summarise_mod.run_full_summarisation")
    def test_run_dream_idempotent(self, mock_summarise, mock_llm):
        """Running dream twice with no new data should be a no-op."""
        mock_summarise.return_value = {"depth_0_created": 0, "cascaded_created": 0}
        mock_llm.return_value = "[CORRECTION] Test. (Source: msg:1)\n"

        config = db.load_config()
        # First run
        dream.run_dream("project", "/tmp/dreamproject", config)
        # Second run — should find no new data
        result = dream.run_dream("project", "/tmp/dreamproject", config)
        self.assertIn("Nothing to dream about", result)


if __name__ == "__main__":
    unittest.main()
