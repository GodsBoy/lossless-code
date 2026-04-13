#!/usr/bin/env python3
"""Tests for the fingerprint file-context feature (PR-B/2..PR-B/5)."""

import importlib
import json
import os
import sys
import tempfile
import unittest

TEST_DIR = tempfile.mkdtemp(prefix="lossless_fingerprint_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db  # noqa: E402


class TestSchemaMigrations(unittest.TestCase):
    """PR-B/2 — messages.file_path, summaries.kind, new indexes."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.LOSSLESS_HOME
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_messages_has_file_path_column(self):
        cols = [r[1] for r in db.get_db().execute("PRAGMA table_info(messages)").fetchall()]
        self.assertIn("file_path", cols)

    def test_summaries_has_kind_column(self):
        cols = [r[1] for r in db.get_db().execute("PRAGMA table_info(summaries)").fetchall()]
        self.assertIn("kind", cols)

    def test_file_path_index_exists(self):
        rows = db.get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_messages_file_path",),
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_summary_sources_composite_index_exists(self):
        rows = db.get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_summary_sources_source",),
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_migration_is_idempotent(self):
        # Re-running get_db() on an existing schema must not raise.
        db.close_db()
        db._conn = None
        db.get_db()  # should not raise
        cols = [r[1] for r in db.get_db().execute("PRAGMA table_info(messages)").fetchall()]
        self.assertIn("file_path", cols)


class TestStoreMessageFilePath(unittest.TestCase):
    """PR-B/2 / PR-B/3 — store_message accepts and persists file_path."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.get_db()

    def test_store_message_with_file_path(self):
        db.ensure_session("fp-session", "/tmp/proj")
        mid = db.store_message(
            session_id="fp-session",
            role="tool",
            content="Read: foo.py (ok)",
            tool_name="Read",
            working_dir="/tmp/proj",
            file_path="foo.py",
        )
        row = db.get_db().execute(
            "SELECT file_path FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        self.assertEqual(row["file_path"], "foo.py")

    def test_store_message_file_path_defaults_to_null(self):
        db.ensure_session("fp-default-session", "/tmp/proj")
        mid = db.store_message(
            session_id="fp-default-session",
            role="user",
            content="no file here",
        )
        row = db.get_db().execute(
            "SELECT file_path FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        self.assertIsNone(row["file_path"])


class TestHookStoreToolCall(unittest.TestCase):
    """PR-B/3 — hook_store_tool_call.py payload parsing and path normalization."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.get_db()
        # Enable the feature flag for these tests.
        cfg = db.load_config()
        cfg["fileContextEnabled"] = True
        db.save_config(cfg)
        # Import the hook module by file path (scripts/ is not a package).
        import importlib.util
        here = os.path.dirname(__file__)
        spec = importlib.util.spec_from_file_location(
            "hook_store_tool_call",
            os.path.join(here, "..", "scripts", "hook_store_tool_call.py"),
        )
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    @classmethod
    def tearDownClass(cls):
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)

    def test_extract_file_path_for_read_tool(self):
        fp = self.hook._extract_file_path("Read", {"file_path": "/abs/foo.py"})
        self.assertEqual(fp, "/abs/foo.py")

    def test_extract_file_path_ignores_unknown_tool(self):
        fp = self.hook._extract_file_path("Bash", {"command": "ls"})
        self.assertIsNone(fp)

    def test_extract_file_path_notebook(self):
        fp = self.hook._extract_file_path(
            "NotebookEdit", {"notebook_path": "/abs/nb.ipynb"}
        )
        self.assertEqual(fp, "/abs/nb.ipynb")

    def test_normalize_path_repo_relative(self):
        with tempfile.TemporaryDirectory() as cwd:
            target = os.path.join(cwd, "subdir", "foo.py")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "w").close()
            rel = self.hook._normalize_path(target, cwd)
            self.assertEqual(rel, os.path.join("subdir", "foo.py"))

    def test_normalize_path_outside_cwd_stays_absolute(self):
        with tempfile.TemporaryDirectory() as cwd:
            abs_outside = "/etc/hostname"
            out = self.hook._normalize_path(abs_outside, cwd)
            self.assertTrue(os.path.isabs(out))


if __name__ == "__main__":
    unittest.main()
