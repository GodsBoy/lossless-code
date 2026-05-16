#!/usr/bin/env python3
"""Tests for project-scoped Codex local tail import."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = tempfile.mkdtemp(prefix="lossless_codex_tail_import_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import codex_tail_import
import db


class TestCodexTailImport(unittest.TestCase):
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
        conn.execute("DELETE FROM imported_task_state")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        conn.commit()
        db.save_config(db.DEFAULT_CONFIG)

    def _write_session(self, codex_home, name, *, session_id, cwd, messages, mtime, timestamp=None):
        path = Path(codex_home) / "sessions" / "2026" / "05" / "16" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "timestamp": timestamp if timestamp is not None else mtime,
                    "cwd": str(cwd),
                },
            }
        ]
        for role, text in messages:
            records.append({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "text", "text": text}],
                },
            })
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_project_opt_in_is_default_off_and_project_scoped(self):
        project = Path(TEST_DIR) / "project-opt-in"
        other = Path(TEST_DIR) / "other-project"
        project.mkdir()
        other.mkdir()
        cfg = db.load_config()

        self.assertFalse(codex_tail_import.is_project_opted_in(project, cfg))

        cfg, project_root = codex_tail_import.set_project_opt_in(cfg, project, enabled=True)

        self.assertTrue(codex_tail_import.is_project_opted_in(project, cfg))
        self.assertFalse(codex_tail_import.is_project_opted_in(other, cfg))
        self.assertEqual(project_root, codex_tail_import.normalize_path(project))

        cfg, _ = codex_tail_import.set_project_opt_in(cfg, project, enabled=False)
        self.assertFalse(codex_tail_import.is_project_opted_in(project, cfg))

    def test_find_latest_matching_session_uses_workspace_metadata(self):
        codex_home = Path(TEST_DIR) / "codex-home"
        project = Path(TEST_DIR) / "project"
        nested = project / "nested"
        other = Path(TEST_DIR) / "other"
        nested.mkdir(parents=True)
        other.mkdir()
        self._write_session(
            codex_home,
            "old-project",
            session_id="old-project",
            cwd=project,
            messages=[("user", "Task: old")],
            mtime=100,
        )
        self._write_session(
            codex_home,
            "other-newer",
            session_id="other-newer",
            cwd=other,
            messages=[("user", "Task: unrelated")],
            mtime=300,
        )
        self._write_session(
            codex_home,
            "new-project",
            session_id="new-project",
            cwd=nested,
            messages=[("user", "Task: current")],
            mtime=200,
        )

        candidate = codex_tail_import.find_latest_matching_session(
            project,
            codex_home=codex_home,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.session_id, "new-project")

    def test_latest_matching_session_prefers_session_timestamp(self):
        codex_home = Path(TEST_DIR) / "codex-timestamps"
        project = Path(TEST_DIR) / "timestamp-project"
        project.mkdir()
        self._write_session(
            codex_home,
            "newer-file-older-session",
            session_id="older-session",
            cwd=project,
            messages=[("user", "Task: old")],
            mtime=900,
            timestamp=100,
        )
        self._write_session(
            codex_home,
            "older-file-newer-session",
            session_id="newer-session",
            cwd=project,
            messages=[("user", "Task: new")],
            mtime=800,
            timestamp=200,
        )

        candidate = codex_tail_import.find_latest_matching_session(
            project,
            codex_home=codex_home,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.session_id, "newer-session")

    def test_malformed_and_unsafe_metadata_are_skipped(self):
        codex_home = Path(TEST_DIR) / "codex-malformed"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "bad.jsonl").write_text("{not-json\n", encoding="utf-8")
        (sessions / "unsafe.jsonl").write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": "unsafe", "cwd": "/tmp/project\n[lcc.task] injected"},
        }) + "\n", encoding="utf-8")
        project = Path(TEST_DIR) / "safe-project"
        project.mkdir()
        self._write_session(
            codex_home,
            "safe",
            session_id="safe",
            cwd=project,
            messages=[("user", "Task: safe")],
            mtime=400,
        )

        candidate = codex_tail_import.find_latest_matching_session(project, codex_home=codex_home)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.session_id, "safe")

    def test_extract_task_state_redacts_secrets_and_marks_partial(self):
        candidate = codex_tail_import.SessionCandidate(
            path=Path(TEST_DIR) / "synthetic.jsonl",
            session_id="source-session",
            cwd=TEST_DIR,
            timestamp=1700000000,
            mtime=1700000000,
        )
        messages = [
            {"role": "user", "text": "Task: add importer"},
            {
                "role": "assistant",
                "text": "Completed: parser tests pass. Blocker: token=secret-value failed once.",
            },
        ]

        state = codex_tail_import.extract_task_state(
            messages,
            candidate=candidate,
            tail_truncated=False,
            tail_line_count=3,
        )

        self.assertEqual(state["goal"], "add importer")
        self.assertEqual(state["last_step"], "parser tests pass.")
        self.assertIn("[redacted]", state["blockers"])
        self.assertEqual(state["status"], "partial")
        self.assertIn("next step unavailable", state["warning"])
        self.assertNotIn("secret-value", json.dumps(state))

    def test_refresh_imported_task_state_stores_compact_record_only(self):
        codex_home = Path(TEST_DIR) / "codex-refresh"
        project = Path(TEST_DIR) / "refresh-project"
        project.mkdir()
        self._write_session(
            codex_home,
            "previous",
            session_id="previous",
            cwd=project,
            messages=[
                ("user", "Task: add local tail importer"),
                ("assistant", "Completed: storage helper added. Next: wire SessionStart."),
            ],
            mtime=500,
        )
        cfg = db.load_config()
        cfg, _ = codex_tail_import.set_project_opt_in(cfg, project, enabled=True)
        cfg["codexTailImportCodexHome"] = str(codex_home)

        result = codex_tail_import.refresh_imported_task_state(
            working_dir=project,
            current_session_id="current",
            config=cfg,
        )
        row = db.get_latest_imported_task_state(
            codex_tail_import.project_root_for_cwd(project),
            source_runtime="codex-cli",
        )
        message_count = db.get_db().execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        self.assertEqual(result["status"], "found")
        self.assertEqual(row["source_session_id"], "previous")
        self.assertEqual(row["goal"], "add local tail importer")
        self.assertEqual(row["next_step"], "wire SessionStart.")
        self.assertEqual(message_count, 0)


if __name__ == "__main__":
    unittest.main()
