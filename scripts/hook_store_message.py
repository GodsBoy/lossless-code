#!/usr/bin/env python3
"""Hook helper: store a message in the vault."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--role", required=True, choices=["user", "assistant", "tool"])
    parser.add_argument("--content", required=True)
    parser.add_argument("--tool-name", default="")
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    db.ensure_session(args.session, args.dir)
    db.store_message(
        session_id=args.session,
        role=args.role,
        content=args.content,
        tool_name=args.tool_name,
        working_dir=args.dir,
    )


if __name__ == "__main__":
    main()
