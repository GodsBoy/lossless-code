#!/usr/bin/env python3
"""Hook helper: persist turn data on Stop event."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    # Read any additional data from stdin if available
    content = ""
    if not sys.stdin.isatty():
        try:
            data = json.load(sys.stdin)
            # Try to extract the assistant's response content
            content = data.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
        except Exception:
            pass

    db.ensure_session(args.session, args.dir)

    # Store a marker message for the stop event if no content
    if not content:
        content = "[Turn completed]"

    db.store_message(
        session_id=args.session,
        role="assistant",
        content=content,
        working_dir=args.dir,
    )


if __name__ == "__main__":
    main()
