#!/usr/bin/env python3
"""Tests for lossless-code dream database functions."""

import os
import sys
import tempfile
import time
import unittest

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_dream_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db


class TestDreamDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Initialise test database."""
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DIR = db.Path(TEST_DIR)
        db.VAULT_DB = db.VAULT_DIR / "vault.db"
        db.CONFIG_PATH = db.VAULT_DIR / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_dream_log_table_exists(self):
        conn = db.get_db()
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("dream_log", tables)

    def test_consolidated_column_exists(self):
        conn = db.get_db()
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(summaries)").fetchall()
        ]
        self.assertIn("consolidated", cols)

    def test_consolidated_column_migration_idempotent(self):
        """Running get_db() again doesn't crash on duplicate column."""
        db.close_db()
        db._conn = None
        db.get_db()  # Should not raise
        conn = db.get_db()
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(summaries)").fetchall()
        ]
        self.assertIn("consolidated", cols)

    def test_project_hash_deterministic(self):
        h1 = db.project_hash("/tmp/myproject")
        h2 = db.project_hash("/tmp/myproject")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_project_hash_different_paths(self):
        h1 = db.project_hash("/tmp/project-a")
        h2 = db.project_hash("/tmp/project-b")
        self.assertNotEqual(h1, h2)

    def test_store_and_get_dream_log(self):
        phash = db.project_hash("/tmp/test-dream")
        row_id = db.store_dream_log(
            project_hash_val=phash,
            scope="project",
            patterns_found=5,
            consolidations=2,
            sessions_analyzed=10,
            report_path="/tmp/report.md",
        )
        self.assertGreater(row_id, 0)

        last = db.get_last_dream(phash)
        self.assertIsNotNone(last)
        self.assertEqual(last["project_hash"], phash)
        self.assertEqual(last["patterns_found"], 5)
        self.assertEqual(last["consolidations"], 2)
        self.assertEqual(last["sessions_analyzed"], 10)
        self.assertEqual(last["scope"], "project")

    def test_get_last_dream_nonexistent(self):
        result = db.get_last_dream("nonexistent_hash")
        self.assertIsNone(result)

    def test_get_last_dream_returns_most_recent(self):
        phash = db.project_hash("/tmp/multi-dream")
        now = int(time.time())
        db.store_dream_log(phash, "project", 1, 0, 3, dreamed_at=now - 100)
        db.store_dream_log(phash, "project", 8, 4, 12, dreamed_at=now)

        last = db.get_last_dream(phash)
        self.assertEqual(last["patterns_found"], 8)

    def test_get_messages_since(self):
        db.ensure_session("dream-msg-session", "/tmp/dreamdir")
        past = int(time.time()) - 10
        db.store_message("dream-msg-session", "user", "old msg", working_dir="/tmp/dreamdir")
        now = int(time.time())
        db.store_message("dream-msg-session", "user", "new msg", working_dir="/tmp/dreamdir")

        msgs = db.get_messages_since(past, "/tmp/dreamdir")
        self.assertGreaterEqual(len(msgs), 1)

    def test_get_messages_since_with_dir_filter(self):
        db.ensure_session("dir-filter-session", "/tmp/specific")
        db.store_message("dir-filter-session", "user", "specific dir msg", working_dir="/tmp/specific")

        db.ensure_session("other-session", "/tmp/other")
        db.store_message("other-session", "user", "other dir msg", working_dir="/tmp/other")

        past = int(time.time()) - 100
        msgs = db.get_messages_since(past, "/tmp/specific")
        for m in msgs:
            self.assertEqual(m["working_dir"], "/tmp/specific")

    def test_get_summaries_since(self):
        past = int(time.time()) - 10
        sid = db.gen_summary_id()
        db.store_summary(sid, "test summary for dream", 0, [], "dream-msg-session")

        summaries = db.get_summaries_since(past)
        ids = [s["id"] for s in summaries]
        self.assertIn(sid, ids)

    def test_mark_consolidated(self):
        sid1 = db.gen_summary_id()
        sid2 = db.gen_summary_id()
        db.store_summary(sid1, "consolidation test 1", 0, [])
        db.store_summary(sid2, "consolidation test 2", 0, [])

        db.mark_consolidated([sid1, sid2])

        s1 = db.get_summary(sid1)
        s2 = db.get_summary(sid2)
        self.assertEqual(s1["consolidated"], 1)
        self.assertEqual(s2["consolidated"], 1)

    def test_mark_consolidated_idempotent(self):
        sid = db.gen_summary_id()
        db.store_summary(sid, "idempotent test", 0, [])
        db.mark_consolidated([sid])
        db.mark_consolidated([sid])  # Should not raise
        s = db.get_summary(sid)
        self.assertEqual(s["consolidated"], 1)

    def test_get_overlapping_summaries(self):
        db.ensure_session("overlap-session", "/tmp")
        # Create two messages
        m1 = db.store_message("overlap-session", "user", "overlap msg 1")
        m2 = db.store_message("overlap-session", "user", "overlap msg 2")
        m3 = db.store_message("overlap-session", "user", "overlap msg 3")

        # Create two summaries sharing sources m1, m2
        s1 = db.gen_summary_id()
        s2 = db.gen_summary_id()
        db.store_summary(s1, "summary A", 0, [("message", str(m1)), ("message", str(m2))])
        db.store_summary(s2, "summary B", 0, [("message", str(m1)), ("message", str(m2)), ("message", str(m3))])

        pairs = db.get_overlapping_summaries(0)
        found = any(
            (a == s1 and b == s2) or (a == s2 and b == s1)
            for a, b in pairs
        )
        self.assertTrue(found, f"Expected overlap pair ({s1}, {s2}) not found in {pairs}")

    def test_get_overlapping_summaries_no_overlap(self):
        m4 = db.store_message("overlap-session", "user", "unique msg 4")
        m5 = db.store_message("overlap-session", "user", "unique msg 5")

        s3 = db.gen_summary_id()
        s4 = db.gen_summary_id()
        # Use depth 5 to isolate from other tests
        db.store_summary(s3, "isolated A", 5, [("message", str(m4))])
        db.store_summary(s4, "isolated B", 5, [("message", str(m5))])

        pairs = db.get_overlapping_summaries(5)
        found = any(
            (a == s3 and b == s4) or (a == s4 and b == s3)
            for a, b in pairs
        )
        self.assertFalse(found, "Should not find overlap between non-overlapping summaries")

    def test_count_sessions_since(self):
        past = int(time.time()) - 100
        db.ensure_session("count-dream-1", "/tmp/countdir")
        db.ensure_session("count-dream-2", "/tmp/countdir")

        count = db.count_sessions_since(past, "/tmp/countdir")
        self.assertGreaterEqual(count, 2)

    def test_count_sessions_since_with_dir_filter(self):
        past = int(time.time()) - 100
        db.ensure_session("filtered-1", "/tmp/filtered")
        db.ensure_session("unfiltered-1", "/tmp/unfiltered")

        count_filtered = db.count_sessions_since(past, "/tmp/filtered")
        count_all = db.count_sessions_since(past)
        self.assertGreaterEqual(count_filtered, 1)
        self.assertGreater(count_all, count_filtered)

    def test_dream_config_defaults(self):
        cfg = db.load_config()
        self.assertIn("autoDream", cfg)
        self.assertIn("dreamAfterSessions", cfg)
        self.assertIn("dreamAfterHours", cfg)
        self.assertIn("dreamModel", cfg)
        self.assertIn("dreamTokenBudget", cfg)
        self.assertEqual(cfg["autoDream"], True)
        self.assertEqual(cfg["dreamAfterSessions"], 5)
        self.assertEqual(cfg["dreamTokenBudget"], 2000)


if __name__ == "__main__":
    unittest.main()
