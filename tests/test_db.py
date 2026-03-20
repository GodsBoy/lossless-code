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


if __name__ == "__main__":
    unittest.main()
