#!/usr/bin/env python3
"""Hook helper: store a message in the vault."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


# Map hook --role to v1.2 span_kind.
# Note: the "tool" role in this helper is used both for tool_result-style
# messages and for system-recorded events (e.g., post_compact records the
# compaction event via this helper with role=tool, tool_name=compaction).
# The compaction_event mapping is a special case below.
_ROLE_TO_SPAN_KIND = {
    "user": "user_prompt",
    "assistant": "assistant_reply",
    "tool": "tool_result",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--role", required=True, choices=["user", "assistant", "tool"])
    parser.add_argument("--content", required=True)
    parser.add_argument("--tool-name", default="")
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    cfg = db.load_config()
    if db.matches_any_pattern(args.session, cfg.get("ignoreSessionPatterns", [])):
        return
    stateless = db.matches_any_pattern(args.session, cfg.get("statelessSessionPatterns", []))
    db.ensure_session(args.session, args.dir, stateless=stateless)

    # v1.2 span_kind derivation:
    # - role=tool with tool_name=compaction is a system-recorded compaction_event
    #   (see hooks/post_compact.sh which records compaction via this helper).
    # - All other tool messages are tool_result (the result of a previous call).
    # - user/assistant map directly via _ROLE_TO_SPAN_KIND.
    if args.role == "tool" and args.tool_name == "compaction":
        span_kind = "compaction_event"
    else:
        span_kind = _ROLE_TO_SPAN_KIND.get(args.role)

    # parent_message_id and tool_call_id are NULL for messages stored via
    # this helper. Claude Code's UserPromptSubmit/Stop hook payloads do not
    # currently expose tool_use_id at this layer; future enhancement.
    db.store_message(
        session_id=args.session,
        role=args.role,
        content=args.content,
        tool_name=args.tool_name,
        working_dir=args.dir,
        span_kind=span_kind,
    )


if __name__ == "__main__":
    main()
