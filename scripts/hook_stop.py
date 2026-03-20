#!/usr/bin/env python3
"""Hook helper: persist conversation from transcript on Stop event.

Reads the Claude Code transcript JSONL file, extracts user/assistant messages,
and stores only NEW messages (deduplicating against what's already in vault).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def extract_text_content(message_obj: dict) -> str:
    """Extract text from a message object's content field.

    Content may be a string or an array of content blocks
    (each with "type": "text" and "text").
    """
    content = message_obj.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def parse_transcript(transcript_path: str) -> list[dict]:
    """Parse a Claude Code transcript JSONL file and extract user/assistant messages.

    Returns list of dicts with 'role' and 'content' keys, in order.
    """
    messages = []
    if not transcript_path or not os.path.isfile(transcript_path):
        return messages

    with open(transcript_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")
            if entry_type not in ("user", "assistant"):
                continue

            # Extract role and content
            message_obj = entry.get("message", {})
            if not message_obj:
                # Some entries have content at top level
                content = extract_text_content(entry)
            else:
                content = extract_text_content(message_obj)

            role = message_obj.get("role", entry_type) if message_obj else entry_type

            if content and content.strip():
                messages.append({"role": role, "content": content.strip()})

    return messages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--dir", default="")
    parser.add_argument("--transcript", default="")
    args = parser.parse_args()

    db.ensure_session(args.session, args.dir)

    # Parse transcript to get all messages
    all_messages = parse_transcript(args.transcript)
    if not all_messages:
        return

    # Count existing messages for this session to deduplicate
    existing_count = db.count_session_messages(args.session)

    # Store only new messages (transcript is append-only)
    new_messages = all_messages[existing_count:]
    for msg in new_messages:
        db.store_message(
            session_id=args.session,
            role=msg["role"],
            content=msg["content"],
            working_dir=args.dir,
        )


if __name__ == "__main__":
    main()
