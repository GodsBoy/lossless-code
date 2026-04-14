#!/usr/bin/env python3
"""Tests for scripts/check_summariser_pollution.py regression detector."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_summariser_pollution.py"

POLLUTING_LINE = (
    '{"type":"user","message":{"role":"user","content":"Summarise the following '
    'conversation turns concisely, preserving all key decisions, facts, file paths, '
    'commands and outputs"}}\n'
)
CLEAN_LINE = '{"type":"user","message":{"role":"user","content":"hello world"}}\n'


def _run(projects_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LOSSLESS_CHECK_PROJECTS_DIR"] = str(projects_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _write_jsonl(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class TestCheckSummariserPollution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name)

    def test_zero_polluting_three_clean_exits_0(self):
        bucket = self.projects / "-root-foo"
        for i in range(3):
            _write_jsonl(bucket / f"clean{i}.jsonl", CLEAN_LINE)
        result = _run(self.projects)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_polluting_files_exit_1_and_listed(self):
        bucket = self.projects / "-root-foo"
        _write_jsonl(bucket / "bad1.jsonl", POLLUTING_LINE)
        _write_jsonl(bucket / "bad2.jsonl", POLLUTING_LINE)
        _write_jsonl(bucket / "ok.jsonl", CLEAN_LINE)
        result = _run(self.projects)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad1.jsonl", result.stdout)
        self.assertIn("bad2.jsonl", result.stdout)
        self.assertNotIn("ok.jsonl", result.stdout)

    def test_polluting_in_cli_cwd_bucket_ignored(self):
        bucket = self.projects / "-root--lossless-code--cli-cwd"
        _write_jsonl(bucket / "legit.jsonl", POLLUTING_LINE)
        result = _run(self.projects)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_jsonl_is_clean(self):
        bucket = self.projects / "-root-foo"
        _write_jsonl(bucket / "empty.jsonl", "")
        result = _run(self.projects)
        self.assertEqual(result.returncode, 0)

    def test_malformed_jsonl_does_not_crash(self):
        bucket = self.projects / "-root-foo"
        _write_jsonl(bucket / "junk.jsonl", "not json at all\n{broken")
        result = _run(self.projects)
        self.assertEqual(result.returncode, 0)

    def test_missing_projects_dir_exits_0(self):
        result = _run(self.projects / "does-not-exist")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
