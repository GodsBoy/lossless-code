#!/usr/bin/env python3
"""Tests for scripts/file_context.py — PreToolUse fingerprint lookup."""

import importlib
import json
import os
import sys
import tempfile
import time
import unittest

TEST_DIR = tempfile.mkdtemp(prefix="lossless_file_context_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db  # noqa: E402
import file_context  # noqa: E402


def _reset_vault():
    db._conn = None
    db.LOSSLESS_HOME = db.Path(TEST_DIR)
    db.VAULT_DIR = db.LOSSLESS_HOME
    db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
    db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
    db.get_db()


class TestFileContextFlagGating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_vault()

    def test_returns_empty_when_flag_off(self):
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)
        self.assertEqual(file_context.get_file_fingerprint("foo.py"), "")


class TestFileContextColdAndWarm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _reset_vault()
        cfg = db.load_config()
        cfg["fileContextEnabled"] = True
        db.save_config(cfg)
        # Seed: one message + one summary linking back.
        db.ensure_session("fc-session", "/tmp/proj")
        mid = db.store_message(
            session_id="fc-session",
            role="tool",
            content="Read: src/foo.py (ok)",
            tool_name="Read",
            working_dir="/tmp/proj",
            file_path="src/foo.py",
        )
        sid = db.gen_summary_id()
        db.store_summary(
            summary_id=sid,
            content="Refactored foo.py auth middleware",
            depth=0,
            source_ids=[("message", str(mid))],
            kind="edited",
        )
        # Close the writer connection so the read-only URI path picks up the
        # committed state via a fresh handle.
        db.close_db()
        # Clear cache file between tests to avoid cross-contamination.
        cache_path = file_context._cache_file()
        if cache_path.exists():
            cache_path.unlink()

    @classmethod
    def tearDownClass(cls):
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)

    def setUp(self):
        cache_path = file_context._cache_file()
        if cache_path.exists():
            cache_path.unlink()

    def test_cold_lookup_returns_fingerprint(self):
        out = file_context.get_file_fingerprint("src/foo.py")
        self.assertIn("[lcc] src/foo.py", out)
        self.assertIn("1 prior summaries", out)
        self.assertIn("edited", out)

    def test_cache_hit_is_served_from_json(self):
        # First call populates cache.
        file_context.get_file_fingerprint("src/foo.py")
        # Second call should hit cache; forge a marker in the cache file to
        # prove we are reading from it instead of running cold again.
        cache = file_context._load_cache()
        self.assertIn("src/foo.py", cache)
        cache["src/foo.py"]["output"] = "SENTINEL_FROM_CACHE"
        cache["src/foo.py"]["ts"] = time.time()
        file_context._store_cache(cache)
        out = file_context.get_file_fingerprint("src/foo.py")
        self.assertEqual(out, "SENTINEL_FROM_CACHE")

    def test_expired_cache_triggers_recompute(self):
        cache = file_context._load_cache()
        cache["src/foo.py"] = {"ts": 0, "output": "STALE"}
        file_context._store_cache(cache)
        out = file_context.get_file_fingerprint("src/foo.py")
        self.assertNotEqual(out, "STALE")
        self.assertIn("[lcc] src/foo.py", out)

    def test_unknown_file_returns_empty(self):
        self.assertEqual(
            file_context.get_file_fingerprint("never-touched.py"), ""
        )


if __name__ == "__main__":
    unittest.main()
