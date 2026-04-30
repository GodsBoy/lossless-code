#!/usr/bin/env python3
"""Regression tests for plugin-only CLI command availability."""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from ensure_cli_shims import ensure_cli_shims


class TestPluginCliShims(unittest.TestCase):
    def _make_plugin_root(self, base: Path) -> Path:
        root = base / "plugin"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for command in ("lcc", "lcc_handoff"):
            target = scripts / command
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(target, 0o755)
        return root

    def test_lcc_handoff_runs_from_plugin_style_shim_without_lossless_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugin"
            scripts = root / "scripts"
            bin_dir = Path(tmp) / "bin"
            scripts.mkdir(parents=True)

            wrapper = Path(__file__).resolve().parents[1] / "scripts" / "lcc_handoff"
            shutil.copy(wrapper, scripts / "lcc_handoff")
            os.chmod(scripts / "lcc_handoff", 0o755)
            (scripts / "lcc.py").write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    print("argv=" + " ".join(sys.argv[1:]))
                    print("home=" + os.environ.get("LOSSLESS_HOME", ""))
                    """
                ),
                encoding="utf-8",
            )

            ensure_cli_shims(root, bin_dir)

            env = os.environ.copy()
            env.pop("LOSSLESS_HOME", None)
            env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", "-c", 'lcc_handoff --generate --session "557f8f41-6571-47"'],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        self.assertIn("argv=handoff --generate --session 557f8f41-6571-47", result.stdout)
        self.assertIn(f"home={root}", result.stdout)

    def test_existing_lossless_symlink_is_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = self._make_plugin_root(tmp_path)
            old_root = tmp_path / ".lossless-code"
            old_scripts = old_root / "scripts"
            old_scripts.mkdir(parents=True)
            old_target = old_scripts / "lcc_handoff"
            old_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "lcc_handoff"
            shim.symlink_to(old_target)

            ensure_cli_shims(root, bin_dir)

            self.assertEqual(shim.resolve(strict=False), root / "scripts" / "lcc_handoff")


if __name__ == "__main__":
    unittest.main()
