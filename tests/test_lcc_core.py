#!/usr/bin/env python3
"""Tests for scripts/lcc_core.py status surface (v1.2 U13)."""

import os
import sys
import tempfile
import unittest

TEST_DIR = tempfile.mkdtemp(prefix="lossless_lcc_core_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
import lcc_core


class TestCollectStatusDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def setUp(self):
        conn = db.get_db()
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM summaries")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM dream_log")
        conn.commit()

    def test_dict_includes_all_v12_fields(self):
        """Every v1.2 field must be present even on an empty vault.

        This is the regression guard against silent drift between the
        CLI and MCP status surfaces.
        """
        s = lcc_core.collect_status_dict()
        required_fields = {
            # Vault
            "vault_path", "vault_bytes", "session_count", "message_count",
            "summary_count", "unsummarised_count", "max_summary_depth",
            "consolidated_count",
            # Dream
            "dream_count", "last_dream_at", "last_dream_mode",
            # v1.2 contracts
            "contracts_pending", "contracts_active", "contracts_retracted",
            "contracts_rejected",
            # v1.2 decisions + bundle
            "decisions_count", "bundle_enabled", "bundle_token_budget",
            # Embedding
            "embedding_enabled", "embedding_model",
            # Provider
            "provider", "model", "provider_auto_detected",
            "provider_last_error",
            # File context
            "file_context_enabled", "file_tagged_messages", "distinct_files",
            "fingerprint_cache_count",
        }
        actual = set(s.keys())
        missing = required_fields - actual
        self.assertEqual(missing, set(), f"missing fields: {missing}")

    def test_empty_vault_zero_counts(self):
        s = lcc_core.collect_status_dict()
        self.assertEqual(s["session_count"], 0)
        self.assertEqual(s["message_count"], 0)
        self.assertEqual(s["contracts_pending"], 0)
        self.assertEqual(s["contracts_active"], 0)
        self.assertEqual(s["decisions_count"], 0)
        self.assertEqual(s["dream_count"], 0)

    def test_contract_counts_reflect_status(self):
        a = db.store_contract_candidate(kind="forbid", body="rule a")
        b = db.store_contract_candidate(kind="prefer", body="rule b")
        c = db.store_contract_candidate(kind="forbid", body="rule c")
        db.approve_contract(b)
        db.reject_contract(c)
        s = lcc_core.collect_status_dict()
        self.assertEqual(s["contracts_pending"], 1)
        self.assertEqual(s["contracts_active"], 1)
        self.assertEqual(s["contracts_rejected"], 1)
        self.assertEqual(s["contracts_retracted"], 0)

    def test_decisions_count_reads_kind_decision(self):
        db.ensure_session("dec-test", "/tmp")
        db.store_summary(
            db.gen_summary_id(), "decision A", 0, [], "dec-test", kind="decision"
        )
        db.store_summary(
            db.gen_summary_id(), "decision B", 0, [], "dec-test", kind="decision"
        )
        # Non-decision summary should NOT count
        db.store_summary(
            db.gen_summary_id(), "general note", 0, [], "dec-test", kind=None
        )
        s = lcc_core.collect_status_dict()
        self.assertEqual(s["decisions_count"], 2)

    def test_last_dream_mode_reflects_dream_log(self):
        # Empty dream_log -> None
        s1 = lcc_core.collect_status_dict()
        self.assertIsNone(s1["last_dream_mode"])
        # After a dream cycle records mode='llm'
        db.store_dream_log(
            project_hash_val="ph1", scope="project",
            patterns_found=0, consolidations=0, sessions_analyzed=0,
            mode="llm",
        )
        s2 = lcc_core.collect_status_dict()
        self.assertEqual(s2["last_dream_mode"], "llm")
        # A newer entry overwrites the recency
        db.store_dream_log(
            project_hash_val="ph1", scope="project",
            patterns_found=0, consolidations=0, sessions_analyzed=0,
            mode="extractive",
        )
        s3 = lcc_core.collect_status_dict()
        self.assertEqual(s3["last_dream_mode"], "extractive")

    def test_bundle_enabled_default_true(self):
        s = lcc_core.collect_status_dict()
        self.assertTrue(s["bundle_enabled"])
        self.assertEqual(s["bundle_token_budget"], 1000)

    def test_bundle_disabled_via_config(self):
        # User flips bundleEnabled=false in config.json (v1.2 rollback path)
        cfg = db.load_config()
        cfg["bundleEnabled"] = False
        db.save_config(cfg)
        try:
            s = lcc_core.collect_status_dict()
            self.assertFalse(s["bundle_enabled"])
        finally:
            cfg["bundleEnabled"] = True
            db.save_config(cfg)


class TestFormatStatusHuman(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    def test_renders_v12_lines(self):
        """Every v1.2 line lands in the rendered string."""
        rendered = lcc_core.format_status_human(lcc_core.collect_status_dict())
        # Vault lines (existing)
        self.assertIn("Vault:", rendered)
        self.assertIn("Sessions:", rendered)
        self.assertIn("Messages:", rendered)
        self.assertIn("Summaries:", rendered)
        # v1.2 lines
        self.assertIn("Contracts:", rendered)
        self.assertIn("Decisions:", rendered)
        self.assertIn("Bundle:", rendered)

    def test_disabled_bundle_surfaces_in_render(self):
        cfg = db.load_config()
        cfg["bundleEnabled"] = False
        db.save_config(cfg)
        try:
            rendered = lcc_core.format_status_human(lcc_core.collect_status_dict())
            self.assertIn("DISABLED", rendered)
            self.assertIn("bundleEnabled=false", rendered)
        finally:
            cfg["bundleEnabled"] = True
            db.save_config(cfg)

    def test_dream_mode_in_render_when_present(self):
        db.store_dream_log(
            project_hash_val="phx", scope="project",
            patterns_found=0, consolidations=0, sessions_analyzed=0,
            mode="extractive",
        )
        rendered = lcc_core.format_status_human(lcc_core.collect_status_dict())
        self.assertIn("mode=extractive", rendered)


if __name__ == "__main__":
    unittest.main()
