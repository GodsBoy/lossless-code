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


class TestGetSummariesForFile(unittest.TestCase):
    """PR-B/5 — recursive CTE walks summary_sources upward from messages."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.get_db()

    def _seed(self, file_path: str):
        db.ensure_session("cte-session", "/tmp/cte")
        mid = db.store_message(
            session_id="cte-session",
            role="tool",
            content=f"Read: {file_path} (ok)",
            tool_name="Read",
            working_dir="/tmp/cte",
            file_path=file_path,
        )
        return mid

    def test_direct_leaf_summary(self):
        mid = self._seed("direct.py")
        sid = db.gen_summary_id()
        db.store_summary(
            summary_id=sid,
            content="summary of direct.py read",
            depth=0,
            source_ids=[("message", str(mid))],
            kind="discussed",
        )
        out = db.get_summaries_for_file("direct.py", limit=5)
        ids = [s["id"] for s in out]
        self.assertIn(sid, ids)

    def test_walks_up_one_level(self):
        mid = self._seed("walk.py")
        leaf = db.gen_summary_id()
        db.store_summary(
            summary_id=leaf,
            content="leaf",
            depth=0,
            source_ids=[("message", str(mid))],
            kind="discussed",
        )
        parent = db.gen_summary_id()
        db.store_summary(
            summary_id=parent,
            content="parent",
            depth=1,
            source_ids=[("summary", leaf)],
            kind="discussed",
        )
        out = db.get_summaries_for_file("walk.py", limit=5)
        ids = [s["id"] for s in out]
        self.assertIn(leaf, ids)
        self.assertIn(parent, ids)

    def test_excludes_consolidated(self):
        mid = self._seed("consol.py")
        sid = db.gen_summary_id()
        db.store_summary(
            summary_id=sid,
            content="merged away",
            depth=0,
            source_ids=[("message", str(mid))],
            kind="discussed",
        )
        db.mark_consolidated([sid])
        out = db.get_summaries_for_file("consol.py", limit=5)
        ids = [s["id"] for s in out]
        self.assertNotIn(sid, ids)

    def test_limit_respected(self):
        mid = self._seed("limit.py")
        for _ in range(5):
            db.store_summary(
                summary_id=db.gen_summary_id(),
                content="s",
                depth=0,
                source_ids=[("message", str(mid))],
                kind="discussed",
            )
        out = db.get_summaries_for_file("limit.py", limit=3)
        self.assertLessEqual(len(out), 3)

    def test_unknown_file_returns_empty(self):
        out = db.get_summaries_for_file("nothing-touched-me.py", limit=3)
        self.assertEqual(out, [])


class TestFormatFileFingerprint(unittest.TestCase):
    """PR-B/6 — format_file_fingerprint rendering and truncation."""

    @classmethod
    def setUpClass(cls):
        import inject_context
        cls.fmt = staticmethod(inject_context.format_file_fingerprint)

    def _summary(self, content, kind="edited", created_at=1_700_000_000):
        return {"content": content, "kind": kind, "created_at": created_at}

    def test_empty_summaries_returns_empty_string(self):
        self.assertEqual(self.fmt("foo.py", []), "")

    def test_basic_shape(self):
        out = self.fmt(
            "src/foo.py",
            [self._summary("Refactored auth middleware"), self._summary("Added tests")],
        )
        self.assertIn("[lcc] src/foo.py", out)
        self.assertIn("2 prior summaries", out)
        self.assertIn("polarity:", out)
        self.assertIn("topics:", out)
        self.assertIn('lcc_expand', out)
        self.assertIn('"file": "src/foo.py"', out)

    def test_polarity_counts(self):
        out = self.fmt(
            "a.py",
            [
                self._summary("x", kind="edited"),
                self._summary("y", kind="edited"),
                self._summary("z", kind="discussed"),
            ],
        )
        self.assertIn("edited×2", out)
        self.assertIn("discussed×1", out)

    def test_unknown_polarity_when_no_kinds(self):
        out = self.fmt(
            "a.py", [self._summary("x", kind=None), self._summary("y", kind=None)]
        )
        self.assertIn("polarity: unknown", out)

    def test_truncation_preserves_file_path_and_expand(self):
        # Force over-budget: many summaries with long topics.
        summaries = [
            self._summary("A really long first line " + ("word " * 20), kind="edited")
            for _ in range(10)
        ]
        out = self.fmt("very/long/path/to/file.py", summaries, token_budget=40)
        self.assertIn("[lcc] very/long/path/to/file.py", out)
        self.assertIn("lcc_expand", out)
        self.assertIn('"file": "very/long/path/to/file.py"', out)


class TestPolarityClassification(unittest.TestCase):
    """PR-B/4 — classify_chunk_polarity covers all categories."""

    @classmethod
    def setUpClass(cls):
        import summarise
        cls.classify = staticmethod(summarise.classify_chunk_polarity)

    def _tool(self, name, path="foo.py"):
        return {"role": "tool", "tool_name": name, "file_path": path}

    def test_none_when_no_file_tools(self):
        chunk = [{"role": "user", "content": "hi"}]
        self.assertIsNone(self.classify(chunk))

    def test_created_for_write(self):
        self.assertEqual(self.classify([self._tool("Write")]), "created")

    def test_edited_for_edit(self):
        self.assertEqual(self.classify([self._tool("Edit")]), "edited")

    def test_edited_for_multiedit(self):
        self.assertEqual(self.classify([self._tool("MultiEdit")]), "edited")

    def test_discussed_for_read_only(self):
        self.assertEqual(
            self.classify([self._tool("Read"), self._tool("Read", "bar.py")]),
            "discussed",
        )

    def test_mixed_when_create_and_edit(self):
        self.assertEqual(
            self.classify([self._tool("Write"), self._tool("Edit", "bar.py")]),
            "mixed",
        )

    def test_ignores_file_tools_without_file_path(self):
        chunk = [{"role": "tool", "tool_name": "Read", "file_path": None}]
        self.assertIsNone(self.classify(chunk))


class TestExpandByFile(unittest.TestCase):
    """PR-B/8 — lcc_expand accepts a `file` param (MCP + CLI parity)."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.get_db()
        cfg = db.load_config()
        cfg["fileContextEnabled"] = True
        db.save_config(cfg)
        db.ensure_session("expand-file-session", "/tmp/exp")
        mid = db.store_message(
            session_id="expand-file-session",
            role="tool",
            content="Edit: expand.py (ok)",
            tool_name="Edit",
            working_dir="/tmp/exp",
            file_path="expand.py",
        )
        cls.sid = db.gen_summary_id()
        db.store_summary(
            summary_id=cls.sid,
            content="Rewrote expand.py signature handling",
            depth=0,
            source_ids=[("message", str(mid))],
            kind="edited",
        )

    @classmethod
    def tearDownClass(cls):
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)

    def test_mcp_do_expand_file_returns_summaries(self):
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "mcp")
        )
        import server as mcp_server
        out = mcp_server._do_expand_file("expand.py", limit=5)
        self.assertIn("expand.py", out)
        self.assertIn(self.sid, out)
        self.assertIn("edited", out)

    def test_mcp_do_expand_file_unknown(self):
        import server as mcp_server
        out = mcp_server._do_expand_file("nothing.py")
        self.assertIn("No summaries", out)

    def test_mcp_do_expand_file_gated_on_flag(self):
        import server as mcp_server
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)
        try:
            out = mcp_server._do_expand_file("expand.py")
            self.assertIn("fileContextEnabled", out)
        finally:
            cfg["fileContextEnabled"] = True
            db.save_config(cfg)


class TestStatusFingerprintSurface(unittest.TestCase):
    """PR-B/9 — lcc status surfaces tagged/file counts when flag is on."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.get_db()
        db.ensure_session("status-fp-session", "/tmp/stfp")
        db.store_message(
            session_id="status-fp-session",
            role="tool",
            content="Edit: status_a.py",
            tool_name="Edit",
            working_dir="/tmp/stfp",
            file_path="status_a.py",
        )
        db.store_message(
            session_id="status-fp-session",
            role="tool",
            content="Edit: status_b.py",
            tool_name="Edit",
            working_dir="/tmp/stfp",
            file_path="status_b.py",
        )

    def test_mcp_status_shows_fingerprint_line_when_enabled(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
        import server as mcp_server
        cfg = db.load_config()
        cfg["fileContextEnabled"] = True
        db.save_config(cfg)
        try:
            out = mcp_server._do_status()
            self.assertIn("Fingerprint:", out)
            self.assertIn("tagged messages", out)
        finally:
            cfg["fileContextEnabled"] = False
            db.save_config(cfg)

    def test_mcp_status_hides_fingerprint_line_when_disabled(self):
        import server as mcp_server
        cfg = db.load_config()
        cfg["fileContextEnabled"] = False
        db.save_config(cfg)
        out = mcp_server._do_status()
        self.assertNotIn("Fingerprint:", out)


if __name__ == "__main__":
    unittest.main()
