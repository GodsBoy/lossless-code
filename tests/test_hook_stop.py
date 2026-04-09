#!/usr/bin/env python3
"""Tests for hook_stop transcript parser."""

import json
import os
import sys
import tempfile
import unittest

# Point to test vault
TEST_DIR = tempfile.mkdtemp(prefix="lossless_stop_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db
from hook_stop import extract_text_content, parse_transcript, main as hook_stop_main


class TestExtractTextContent(unittest.TestCase):
    def test_string_content(self):
        msg = {"content": "Hello world"}
        self.assertEqual(extract_text_content(msg), "Hello world")

    def test_array_content(self):
        msg = {
            "content": [
                {"type": "text", "text": "First part"},
                {"type": "text", "text": "Second part"},
            ]
        }
        self.assertEqual(extract_text_content(msg), "First part\nSecond part")

    def test_mixed_array_content(self):
        msg = {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "name": "bash"},
            ]
        }
        self.assertEqual(extract_text_content(msg), "Hello")

    def test_empty_content(self):
        msg = {"content": ""}
        self.assertEqual(extract_text_content(msg), "")

    def test_missing_content(self):
        msg = {}
        self.assertEqual(extract_text_content(msg), "")


class TestParseTranscript(unittest.TestCase):
    def _write_jsonl(self, lines):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for line in lines:
            f.write(json.dumps(line) + "\n")
        f.close()
        return f.name

    def test_basic_transcript(self):
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": "Hello"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Hi there"}},
        ])
        msgs = parse_transcript(path)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Hello")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Hi there")
        os.unlink(path)

    def test_skips_non_message_types(self):
        path = self._write_jsonl([
            {"type": "system", "content": "system stuff"},
            {"type": "user", "message": {"role": "user", "content": "Hello"}},
            {"type": "progress", "data": {}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Hi"}},
            {"type": "file-history-snapshot", "files": []},
        ])
        msgs = parse_transcript(path)
        self.assertEqual(len(msgs), 2)
        os.unlink(path)

    def test_array_content_blocks(self):
        path = self._write_jsonl([
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Part one"},
                        {"type": "text", "text": "Part two"},
                    ],
                },
            }
        ])
        msgs = parse_transcript(path)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "Part one\nPart two")
        os.unlink(path)

    def test_empty_file(self):
        path = self._write_jsonl([])
        msgs = parse_transcript(path)
        self.assertEqual(msgs, [])
        os.unlink(path)

    def test_nonexistent_file(self):
        msgs = parse_transcript("/nonexistent/path.jsonl")
        self.assertEqual(msgs, [])

    def test_empty_path(self):
        msgs = parse_transcript("")
        self.assertEqual(msgs, [])

    def test_skips_empty_content(self):
        path = self._write_jsonl([
            {"type": "user", "message": {"role": "user", "content": ""}},
            {"type": "user", "message": {"role": "user", "content": "Real message"}},
        ])
        msgs = parse_transcript(path)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "Real message")
        os.unlink(path)


class TestHookStopPatternFiltering(unittest.TestCase):
    """Tests that hook_stop.main() respects ignoreSessionPatterns and statelessSessionPatterns."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_ignore_pattern_prevents_session_creation(self):
        """Sessions matching ignoreSessionPatterns are not created by hook_stop."""
        import json
        db.save_config({**db.load_config(), "ignoreSessionPatterns": ["ignore-stop-*"]})
        try:
            import sys
            sys.argv = ["hook_stop", "--session", "ignore-stop-test", "--dir", "/tmp", "--transcript", ""]
            hook_stop_main()
            self.assertIsNone(db.get_session("ignore-stop-test"))
        finally:
            db.save_config({**db.load_config(), "ignoreSessionPatterns": []})

    def test_stateless_pattern_creates_stateless_session(self):
        """Sessions matching statelessSessionPatterns are created with stateless=True by hook_stop."""
        db.save_config({**db.load_config(), "statelessSessionPatterns": ["stateless-stop-*"]})
        try:
            import sys
            sys.argv = ["hook_stop", "--session", "stateless-stop-test", "--dir", "/tmp", "--transcript", ""]
            hook_stop_main()
            self.assertTrue(db.get_session_stateless("stateless-stop-test"))
        finally:
            db.save_config({**db.load_config(), "statelessSessionPatterns": []})


class TestDeduplication(unittest.TestCase):
    """Test that stop hook deduplication logic works correctly."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_dedup_skips_existing(self):
        session_id = "dedup-test"
        db.ensure_session(session_id, "/tmp")

        # Simulate first stop: 2 messages in transcript
        db.store_message(session_id, "user", "msg1")
        db.store_message(session_id, "assistant", "reply1")
        self.assertEqual(db.count_session_messages(session_id), 2)

        # Simulate second stop: transcript now has 4 messages
        all_msgs = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "reply2"},
        ]
        existing = db.count_session_messages(session_id)
        new_msgs = all_msgs[existing:]
        for msg in new_msgs:
            db.store_message(session_id, msg["role"], msg["content"])

        self.assertEqual(db.count_session_messages(session_id), 4)


class TestSessionFiltering(unittest.TestCase):
    """Tests for session pattern filtering (ignore + stateless)."""

    @classmethod
    def setUpClass(cls):
        db._conn = None
        db.LOSSLESS_HOME = db.Path(TEST_DIR)
        db.VAULT_DB = db.LOSSLESS_HOME / "vault.db"
        db.CONFIG_PATH = db.LOSSLESS_HOME / "config.json"
        db.get_db()

    @classmethod
    def tearDownClass(cls):
        db.close_db()

    def test_stateless_session_flagged(self):
        """Session created with stateless=True is correctly flagged."""
        db.ensure_session("sl-test-1", "/tmp", stateless=True)
        self.assertTrue(db.get_session_stateless("sl-test-1"))

    def test_normal_session_not_stateless(self):
        """Session created without stateless flag is normal."""
        db.ensure_session("sl-test-normal", "/tmp")
        self.assertFalse(db.get_session_stateless("sl-test-normal"))

    def test_stateless_messages_excluded_from_get_messages_since(self):
        """Messages from stateless sessions are excluded from dream queries."""
        import time
        ts = int(time.time()) - 1
        db.ensure_session("sl-excluded-session", "/tmp", stateless=True)
        db.store_message("sl-excluded-session", "user", "stateless content xyz")
        db.ensure_session("sl-included-session", "/tmp", stateless=False)
        db.store_message("sl-included-session", "user", "normal content xyz")

        msgs = db.get_messages_since(ts)
        contents = [m["content"] for m in msgs]
        self.assertNotIn("stateless content xyz", contents)
        self.assertIn("normal content xyz", contents)

    def test_stateless_session_excluded_from_get_unsummarised(self):
        """Stateless session messages must not appear in get_unsummarised() — the summarise path."""
        db.ensure_session("sl-nosummary-session", "/tmp", stateless=True)
        db.store_message("sl-nosummary-session", "user", "this is from a stateless session")
        db.ensure_session("sl-normal-session", "/tmp")
        db.store_message("sl-normal-session", "user", "this is from a normal session")

        msgs = db.get_unsummarised()
        contents = [m["content"] for m in msgs]
        self.assertNotIn("this is from a stateless session", contents)
        self.assertIn("this is from a normal session", contents)


if __name__ == "__main__":
    unittest.main()
