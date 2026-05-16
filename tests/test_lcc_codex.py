#!/usr/bin/env python3
"""Tests for lcc Codex support helpers."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = tempfile.mkdtemp(prefix="lossless_lcc_codex_test_")
os.environ["LOSSLESS_HOME"] = TEST_DIR
os.environ["LOSSLESS_VAULT_DIR"] = TEST_DIR

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import codex_support
import db


class Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class TestCodexSupport(unittest.TestCase):
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
        conn.execute("DELETE FROM summary_sources")
        conn.execute("DELETE FROM summaries")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        conn.commit()
        db.save_config(db.DEFAULT_CONFIG)

    def test_hook_config_targets_session_start(self):
        config = codex_support.hook_config(
            python_executable="python",
            script_path="scripts/codex_session_start.py",
        )
        groups = config["hooks"]["SessionStart"]
        self.assertEqual(groups[0]["matcher"], "startup|resume|clear")
        handler = groups[0]["hooks"][0]
        self.assertEqual(handler["type"], "command")
        self.assertIn("codex_session_start.py", handler["command"])
        self.assertEqual(handler["timeout"], 30)

    def test_write_hook_config_merges_existing_file(self):
        root = Path(TEST_DIR) / "project"
        root.mkdir()
        hook_path = root / ".codex" / "hooks.json"
        hook_path.parent.mkdir()
        hook_path.write_text(
            '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}',
            encoding="utf-8",
        )

        written = codex_support.write_hook_config(
            "project",
            cwd=root,
            python_executable="python",
            script_path="scripts/codex_session_start.py",
        )

        self.assertEqual(written, hook_path)
        content = hook_path.read_text(encoding="utf-8")
        self.assertIn('"Stop"', content)
        self.assertIn('"SessionStart"', content)
        self.assertIn("codex_session_start.py", content)

    def test_write_hook_config_preserves_unrelated_session_start_handlers(self):
        root = Path(TEST_DIR) / "project-mixed-hooks"
        root.mkdir()
        hook_path = root / ".codex" / "hooks.json"
        hook_path.parent.mkdir()
        hook_path.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python old/codex_session_start.py",
                            },
                            {
                                "type": "command",
                                "command": "echo keep-me",
                            },
                        ],
                    }
                ]
            }
        }), encoding="utf-8")

        codex_support.write_hook_config(
            "project",
            cwd=root,
            python_executable="python",
            script_path="scripts/codex_session_start.py",
        )

        config = json.loads(hook_path.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for group in config["hooks"]["SessionStart"]
            for hook in group["hooks"]
        ]
        normalized = [command.replace("\\", "/") for command in commands]
        self.assertIn("echo keep-me", normalized)
        self.assertIn("python scripts/codex_session_start.py", normalized)
        self.assertNotIn("python old/codex_session_start.py", normalized)

    def test_project_hook_path_uses_git_root(self):
        root = Path(TEST_DIR) / "project-root"
        nested = root / "nested"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

        path = codex_support.hook_config_path("project", cwd=nested)

        self.assertEqual(path, root / ".codex" / "hooks.json")

    def test_lcc_codex_start_print_context_runs_without_dream_import(self):
        cli = Path(__file__).resolve().parent.parent / "scripts" / "lcc.py"
        vault = Path(TEST_DIR) / "cli-vault"
        vault.mkdir()
        env = os.environ.copy()
        env["LOSSLESS_HOME"] = str(vault)
        env["LOSSLESS_VAULT_DIR"] = str(vault)

        proc = subprocess.run(
            [
                sys.executable,
                str(cli),
                "codex",
                "start",
                "--print-context",
                "--cwd",
                str(Path(TEST_DIR)),
                "smoke task",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Lossless-Code recalled context follows", proc.stdout)
        self.assertIn("[lcc.task]", proc.stdout)

    def test_mcp_add_command_registers_lossless_code(self):
        command = codex_support.mcp_add_command(
            codex_cmd="codex",
            python_executable="python",
            server_path="mcp/server.py",
        )
        self.assertEqual(command[:4], ["codex", "mcp", "add", "lossless-code"])
        self.assertTrue(any(part.replace("\\", "/") == "mcp/server.py" for part in command))

    def test_doctor_reports_ready_and_missing_mcp(self):
        def runner(args, timeout=5):
            joined = " ".join(args)
            if "--version" in joined:
                return Proc("codex-cli 0.130.0-alpha.5\n")
            if "features list" in joined:
                return Proc(
                    "hooks stable true\nmcp stable true\nplugin_hooks under development false\n"
                )
            if "mcp list" in joined:
                return Proc("No MCP servers configured yet.\n")
            return Proc("")

        checks = codex_support.collect_doctor_checks(
            codex_cmd=sys.executable,
            codex_home=Path(TEST_DIR) / "codex-home",
            cwd=Path(TEST_DIR),
            runner=runner,
        )
        rendered = codex_support.format_checks(checks)
        self.assertIn("[ok] Codex CLI", rendered)
        self.assertIn("[ok] Hooks feature: true", rendered)
        self.assertIn("[warn] MCP registration: lossless-code not configured", rendered)
        self.assertIn("[ok] Bundle preview", rendered)

    def test_doctor_warns_when_hooks_file_lacks_codex_registration(self):
        root = Path(TEST_DIR) / "project-unrelated-hook"
        hook_path = root / ".codex" / "hooks.json"
        hook_path.parent.mkdir(parents=True)
        hook_path.write_text(
            '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}}',
            encoding="utf-8",
        )

        checks = codex_support.collect_doctor_checks(
            codex_cmd=sys.executable,
            codex_home=Path(TEST_DIR) / "empty-codex-home",
            cwd=root,
            runner=lambda args, timeout=5: Proc(""),
        )

        rendered = codex_support.format_checks(checks)
        self.assertIn("[warn] Hook config: codex_session_start.py not registered", rendered)

    def test_doctor_reports_registered_codex_hook(self):
        root = Path(TEST_DIR) / "project-registered-hook"
        codex_support.write_hook_config(
            "project",
            cwd=root,
            python_executable="python",
            script_path="scripts/codex_session_start.py",
        )

        checks = codex_support.collect_doctor_checks(
            codex_cmd=sys.executable,
            codex_home=Path(TEST_DIR) / "empty-codex-home-2",
            cwd=root,
            runner=lambda args, timeout=5: Proc(""),
        )

        rendered = codex_support.format_checks(checks)
        self.assertIn("[ok] Hook config: codex_session_start.py registered", rendered)

    def test_doctor_handles_invalid_absolute_codex_path(self):
        checks = codex_support.collect_doctor_checks(
            codex_cmd=str(Path(TEST_DIR) / "missing-codex.exe"),
        )
        rendered = codex_support.format_checks(checks)
        self.assertIn("[fail] Codex CLI", rendered)

    def test_doctor_handles_runner_errors(self):
        def runner(args, timeout=5):
            raise subprocess.TimeoutExpired(args, timeout)

        checks = codex_support.collect_doctor_checks(
            codex_cmd=sys.executable,
            codex_home=Path(TEST_DIR) / "empty-codex-home-3",
            cwd=Path(TEST_DIR),
            runner=runner,
        )

        rendered = codex_support.format_checks(checks)
        self.assertIn("[warn] Codex version: could not read version", rendered)
        self.assertIn("[warn] Feature list: could not read feature flags", rendered)
        self.assertIn("[warn] MCP registration: could not list MCP servers", rendered)

    def test_doctor_handles_missing_codex(self):
        checks = codex_support.collect_doctor_checks(codex_cmd="codex-definitely-missing")
        rendered = codex_support.format_checks(checks)
        self.assertIn("[fail] Codex CLI", rendered)
        self.assertIn("[fail] Launcher fallback", rendered)

    def test_launcher_prompt_contains_lower_authority_context_and_user_prompt(self):
        prompt = codex_support.build_launcher_prompt(
            "continue the feature",
            cwd=Path(TEST_DIR),
        )
        self.assertIn("Lossless-Code recalled context follows", prompt)
        self.assertIn("[lcc.task]", prompt)
        self.assertIn("Current user prompt:", prompt)
        self.assertIn("continue the feature", prompt)

    def test_launch_missing_codex_returns_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = codex_support.launch_codex_with_context(
                "hello",
                codex_cmd="codex-definitely-missing",
                cwd=Path(TEST_DIR),
            )
        self.assertEqual(code, 1)
        self.assertIn("Codex CLI not found", stderr.getvalue())

    def test_installer_copies_codex_support_files(self):
        install_sh = Path(__file__).resolve().parent.parent / "install.sh"
        content = install_sh.read_text(encoding="utf-8")
        self.assertIn("codex_support.py", content)
        self.assertIn("codex_session_start.py", content)


if __name__ == "__main__":
    unittest.main()
