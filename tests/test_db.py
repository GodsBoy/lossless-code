#!/usr/bin/env python3
"""Tests for lossless-code database layer."""

import os
import sys
import tempfile
import unittest

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db


class TestDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Initialise test database."""
        db._conn = None  # Reset connection
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_schema_tables_exist(self):
        conn = db.get_db()
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for expected in ["sessions", "messages", "summaries", "summary_sources"]:
            self.assertIn(expected, tables)

    def test_import_surface_matches_all(self):
        """Every name in db.__all__ must resolve on the package.

        Locks in the flat-namespace contract: callers use ``import db; db.foo()``
        and a future refactor that silently drops a re-export would break them
        only at runtime. Cheap regression guard for the db package split.
        """
        missing = [name for name in db.__all__ if not hasattr(db, name)]
        self.assertEqual(missing, [], f"db.__all__ references missing names: {missing}")

    def test_fts_tables_exist(self):
        conn = db.get_db()
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("messages_fts", tables)
        self.assertIn("summaries_fts", tables)

    def test_session_lifecycle(self):
        db.ensure_session("test-session-1", "/tmp/test")
        session = db.get_session("test-session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session["session_id"], "test-session-1")
        self.assertEqual(session["working_dir"], "/tmp/test")

    def test_session_idempotent(self):
        db.ensure_session("test-session-2", "/tmp/a")
        db.ensure_session("test-session-2", "/tmp/a")
        sessions = db.list_sessions()
        ids = [s["session_id"] for s in sessions]
        self.assertEqual(ids.count("test-session-2"), 1)

    def test_store_and_retrieve_message(self):
        db.ensure_session("msg-session", "/tmp")
        msg_id = db.store_message(
            session_id="msg-session",
            role="user",
            content="Hello, this is a test message",
            working_dir="/tmp",
        )
        self.assertIsNotNone(msg_id)
        self.assertGreater(msg_id, 0)

    def test_unsummarised_messages(self):
        db.ensure_session("unsum-session", "/tmp")
        db.store_message("unsum-session", "user", "message one")
        db.store_message("unsum-session", "assistant", "response one")

        unsummarised = db.get_unsummarised("unsum-session")
        self.assertGreaterEqual(len(unsummarised), 2)

    def test_mark_summarised(self):
        db.ensure_session("mark-session", "/tmp")
        id1 = db.store_message("mark-session", "user", "msg to summarise")
        id2 = db.store_message("mark-session", "assistant", "response to summarise")

        db.mark_summarised([id1, id2])

        unsummarised = db.get_unsummarised("mark-session")
        unsummarised_ids = [m["id"] for m in unsummarised]
        self.assertNotIn(id1, unsummarised_ids)
        self.assertNotIn(id2, unsummarised_ids)

    def test_summary_and_sources(self):
        db.ensure_session("sum-session", "/tmp")
        id1 = db.store_message("sum-session", "user", "question about auth")
        id2 = db.store_message("sum-session", "assistant", "auth uses JWT tokens")

        summary_id = db.gen_summary_id()
        self.assertTrue(summary_id.startswith("sum_"))

        db.store_summary(
            summary_id=summary_id,
            content="Discussion about auth: uses JWT tokens.",
            depth=0,
            source_ids=[("message", str(id1)), ("message", str(id2))],
            session_id="sum-session",
            token_count=10,
        )

        summary = db.get_summary(summary_id)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["depth"], 0)
        self.assertEqual(summary["session_id"], "sum-session")

        sources = db.get_summary_sources(summary_id)
        self.assertEqual(len(sources), 2)

    def test_depth_cascade_summaries(self):
        db.ensure_session("depth-session", "/tmp")

        # Create multiple depth-0 summaries
        for i in range(3):
            sid = db.gen_summary_id()
            db.store_summary(
                summary_id=sid,
                content=f"Summary {i} at depth 0",
                depth=0,
                source_ids=[],
                session_id="depth-session",
            )

        depth0 = db.get_summaries_at_depth(0, "depth-session")
        self.assertEqual(len(depth0), 3)

    def test_top_summaries(self):
        db.ensure_session("top-session", "/tmp")
        # depth 0
        db.store_summary(db.gen_summary_id(), "low depth", 0, [], "top-session")
        # depth 1
        db.store_summary(db.gen_summary_id(), "high depth", 1, [], "top-session")

        top = db.get_top_summaries(limit=1, session_id="top-session")
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["depth"], 1)

    def test_fts_search_messages(self):
        db.ensure_session("fts-session", "/tmp")
        db.store_message("fts-session", "user", "The kangaroo jumped over the database")

        results = db.search_messages("kangaroo")
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("kangaroo", results[0]["content"])

    def test_fts_search_summaries(self):
        db.store_summary(
            db.gen_summary_id(),
            "Discussed the zebra migration pattern in detail",
            0,
            [],
            "fts-session",
        )

        results = db.search_summaries("zebra migration")
        self.assertGreaterEqual(len(results), 1)

    def test_search_all(self):
        results = db.search_all("kangaroo")
        self.assertIn("messages", results)
        self.assertIn("summaries", results)

    def test_config_roundtrip(self):
        cfg = db.load_config()
        self.assertIn("chunkSize", cfg)
        self.assertEqual(cfg["chunkSize"], 20)

        cfg["chunkSize"] = 30
        db.save_config(cfg)

        cfg2 = db.load_config()
        self.assertEqual(cfg2["chunkSize"], 30)

        # Restore
        cfg["chunkSize"] = 20
        db.save_config(cfg)

    def test_handoff(self):
        db.ensure_session("handoff-session", "/tmp")
        db.set_handoff("handoff-session", "Left off at implementing auth module")

        session = db.get_session("handoff-session")
        self.assertEqual(session["handoff_text"], "Left off at implementing auth module")

    def test_get_messages_by_ids(self):
        db.ensure_session("byid-session", "/tmp")
        id1 = db.store_message("byid-session", "user", "first msg")
        id2 = db.store_message("byid-session", "assistant", "second msg")

        msgs = db.get_messages_by_ids([id1, id2])
        self.assertEqual(len(msgs), 2)

    # --- Bug 2: FTS5 special character escaping ---

    def test_escape_fts5_basic(self):
        result = db.escape_fts5_query("hello world")
        self.assertEqual(result, '"hello" "world"')

    def test_escape_fts5_question_mark(self):
        result = db.escape_fts5_query("What was built?")
        self.assertEqual(result, '"What" "was" "built"')

    def test_escape_fts5_special_chars(self):
        result = db.escape_fts5_query('test * (foo) "bar" ^baz +qux -quux ~corge :grault')
        self.assertNotIn("*", result)
        self.assertNotIn("(", result)
        self.assertNotIn(")", result)
        self.assertNotIn("^", result)
        self.assertNotIn("+", result.replace('"', ""))  # quotes are expected
        self.assertNotIn("~", result)

    def test_escape_fts5_empty(self):
        result = db.escape_fts5_query("???")
        self.assertEqual(result, "")

    def test_fts_search_with_special_chars(self):
        """Queries with special chars should not crash."""
        db.ensure_session("fts-special", "/tmp")
        db.store_message("fts-special", "user", "What was the magic number built here")

        # These previously crashed with sqlite3.OperationalError
        results = db.search_messages("What was built?")
        self.assertIsInstance(results, list)

        results = db.search_messages("magic number (42)")
        self.assertIsInstance(results, list)

        results = db.search_messages("test*query")
        self.assertIsInstance(results, list)

    def test_fts_search_empty_after_escape(self):
        """Query that becomes empty after escaping returns empty list."""
        results = db.search_messages("???")
        self.assertEqual(results, [])

    # --- Bug 1: count_session_messages ---

    def test_count_session_messages(self):
        db.ensure_session("count-session", "/tmp")
        self.assertEqual(db.count_session_messages("count-session"), 0)

        db.store_message("count-session", "user", "msg one")
        self.assertEqual(db.count_session_messages("count-session"), 1)

        db.store_message("count-session", "assistant", "msg two")
        self.assertEqual(db.count_session_messages("count-session"), 2)

    def test_count_session_messages_nonexistent(self):
        self.assertEqual(db.count_session_messages("nonexistent-session"), 0)

    # --- Session filtering (lossless-claw parity) ---

    def test_stateless_column_exists(self):
        conn = db.get_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        self.assertIn("stateless", cols)

    def test_ensure_session_stateless_default_false(self):
        db.ensure_session("normal-session-filter-test", "/tmp")
        self.assertFalse(db.get_session_stateless("normal-session-filter-test"))

    def test_ensure_session_stateless_true(self):
        db.ensure_session("stateless-session-1", "/tmp", stateless=True)
        self.assertTrue(db.get_session_stateless("stateless-session-1"))

    def test_get_session_stateless_nonexistent(self):
        self.assertFalse(db.get_session_stateless("no-such-session"))

    def test_matches_any_pattern_exact(self):
        self.assertTrue(db.matches_any_pattern("abc-session", ["abc-session"]))
        self.assertFalse(db.matches_any_pattern("abc-session", ["xyz-session"]))

    def test_matches_any_pattern_wildcard(self):
        self.assertTrue(db.matches_any_pattern("agent:foo:subagent:bar", ["agent:*:subagent:*"]))
        self.assertFalse(db.matches_any_pattern("agent:foo:bar", ["agent:*:subagent:*"]))

    def test_matches_any_pattern_empty_list(self):
        self.assertFalse(db.matches_any_pattern("any-session", []))

    def test_matches_any_pattern_multiple(self):
        patterns = ["cron:*", "agent:*:subagent:*"]
        self.assertTrue(db.matches_any_pattern("cron:nightly", patterns))
        self.assertTrue(db.matches_any_pattern("agent:foo:subagent:1", patterns))
        self.assertFalse(db.matches_any_pattern("user-session-123", patterns))

    def test_get_messages_since_excludes_stateless(self):
        import time
        ts = int(time.time()) - 1
        db.ensure_session("stateless-msg-session", "/tmp/stateless", stateless=True)
        db.store_message("stateless-msg-session", "user", "should be excluded")
        db.ensure_session("normal-msg-session", "/tmp/normal")
        db.store_message("normal-msg-session", "user", "should be included")

        messages = db.get_messages_since(ts)
        contents = [m["content"] for m in messages]
        self.assertIn("should be included", contents)
        self.assertNotIn("should be excluded", contents)

    def test_get_unsummarised_excludes_stateless(self):
        """Stateless session messages must not enter the summarise path."""
        db.ensure_session("stateless-unsummarised", "/tmp", stateless=True)
        db.store_message("stateless-unsummarised", "user", "stateless-should-not-summarise")
        db.ensure_session("normal-unsummarised", "/tmp")
        db.store_message("normal-unsummarised", "user", "normal-should-summarise")

        messages = db.get_unsummarised()
        contents = [m["content"] for m in messages]
        self.assertIn("normal-should-summarise", contents)
        self.assertNotIn("stateless-should-not-summarise", contents)


if __name__ == "__main__":
    unittest.main()
