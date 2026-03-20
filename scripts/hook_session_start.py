#!/usr/bin/env python3
"""Hook helper: ensure session exists on SessionStart."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    db.ensure_session(args.session, args.dir)


if __name__ == "__main__":
    main()
