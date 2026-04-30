#!/usr/bin/env python3
"""Install user PATH shims for plugin-bundled lcc commands."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


COMMANDS = (
    "lcc",
    "lcc_grep",
    "lcc_expand",
    "lcc_context",
    "lcc_sessions",
    "lcc_handoff",
    "lcc_status",
    "lcc_dream",
)


def _resolve_existing_link(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    target = Path(os.readlink(path))
    if not target.is_absolute():
        target = path.parent / target
    return target


def _is_lossless_target(path: Path) -> bool:
    text = str(path)
    return (
        "/lossless-code/" in text
        or "/.lossless-code/scripts/" in text
        or text.endswith("/.lossless-code/scripts")
    )


def ensure_cli_shims(plugin_root: Path, bin_dir: Path) -> list[str]:
    scripts_dir = plugin_root / "scripts"
    bin_dir.mkdir(parents=True, exist_ok=True)

    messages: list[str] = []
    for command in COMMANDS:
        target = scripts_dir / command
        if not target.exists():
            messages.append(f"missing target: {target}")
            continue

        shim = bin_dir / command
        existing_target = _resolve_existing_link(shim)
        if existing_target is not None:
            if existing_target.resolve(strict=False) == target.resolve(strict=False):
                continue
            if _is_lossless_target(existing_target.resolve(strict=False)):
                shim.unlink()
            else:
                messages.append(f"skipped non-lossless shim: {shim}")
                continue
        elif shim.exists():
            messages.append(f"skipped non-symlink command: {shim}")
            continue

        shim.symlink_to(target)
        messages.append(f"linked {shim} -> {target}")

    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plugin-root",
        default=os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("LOSSLESS_HOME"),
    )
    parser.add_argument(
        "--bin-dir",
        default=os.environ.get("LCC_SHIM_BIN_DIR") or str(Path.home() / ".local" / "bin"),
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.plugin_root:
        return 0

    messages = ensure_cli_shims(Path(args.plugin_root).expanduser(), Path(args.bin_dir).expanduser())
    if not args.quiet:
        for message in messages:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
