#!/usr/bin/env python3
"""Hook helper: embed un-indexed messages after a Stop event.

Called as a background process from stop.sh when embeddingEnabled=true.
Mirrors the pattern of hook_stop.py — thin wrapper over the embed module.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import embed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="")
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    cfg = db.load_config()
    if not cfg.get("embeddingEnabled", False):
        return

    session_id = args.session or None
    if session_id and db.get_session_stateless(session_id):
        return

    embed.embed_messages_batch(cfg, session_id=session_id)


if __name__ == "__main__":
    main()
