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


class TestLccExpandCliParity(unittest.TestCase):
    """v1.2 P1: lcc expand --span-id mirrors the MCP span_id mode."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()
        # Re-import lcc with the test paths in place. Import once at class
        # setup so the parser is built against the test environment.
        import importlib
        import lcc as _lcc
        importlib.reload(_lcc)
        cls.lcc = _lcc

    def test_parser_accepts_span_id(self):
        """The argument lands on the namespace under the same name as MCP."""
        parser = None
        # Walk the lcc.main parser surface via build_parser if it exists,
        # otherwise rely on parsing through main(). We exercise the parse
        # path directly by constructing the same parser.
        import argparse
        import lcc as _lcc
        # Use a tiny argv to confirm parsing succeeds.
        old_argv = sys.argv
        try:
            sys.argv = ["lcc", "expand", "--span-id", "42"]
            # Build a parser by introspecting main(). Easier: invoke main()
            # with a recorded args.func that asserts the namespace shape.
            captured = {}
            original = _lcc.cmd_expand_span
            def _capture(args):
                captured["span_id"] = args.span_id
                captured["full"] = getattr(args, "full", False)
            _lcc.cmd_expand_span = _capture
            try:
                _lcc.main()
            finally:
                _lcc.cmd_expand_span = original
        finally:
            sys.argv = old_argv
        self.assertEqual(captured.get("span_id"), "42")

    def test_cli_walks_real_chain(self):
        """End-to-end: seed messages, walk the chain via cmd_expand_span."""
        import io
        import contextlib
        import time as _time
        import lcc as _lcc

        db.ensure_session("cli-span", "/tmp/cli-span")
        conn = db.get_db()
        ts = int(_time.time() * 1000)
        parent = None
        leaf_id = None
        for i in range(3):
            cur = conn.execute(
                "INSERT INTO messages (session_id, turn_id, role, content, "
                "tool_name, working_dir, timestamp, span_kind, parent_message_id) "
                "VALUES (?, '', 'user', ?, '', '', ?, 'user_prompt', ?)",
                ("cli-span", f"hop {i} content", ts + i, parent),
            )
            parent = cur.lastrowid
            leaf_id = cur.lastrowid
        conn.commit()

        buf = io.StringIO()
        ns = type("NS", (), {"span_id": str(leaf_id), "full": False})()
        with contextlib.redirect_stdout(buf):
            _lcc.cmd_expand_span(ns)
        out = buf.getvalue()
        self.assertIn("Span chain", out)
        self.assertIn("hop 0", out)
        self.assertIn("hop 2", out)


if __name__ == "__main__":
    unittest.main()
