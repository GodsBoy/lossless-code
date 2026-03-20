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
from hook_stop import extract_text_content, parse_transcript


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


if __name__ == "__main__":
    unittest.main()
