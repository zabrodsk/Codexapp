#!/usr/bin/env python3
"""
Discord message poster that handles the 2000-character limit.
Splits long reports into multiple messages.
"""
from __future__ import annotations

import json
import sys
import re
from pathlib import Path

# Add workspace to path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def split_activity_report(markdown_content: str) -> list[str]:
    """
    Split activity report into Discord-friendly messages (~1800 chars each to be safe).
    Returns list of message strings.
    """
    messages = []

    # Extract sections
    lines = markdown_content.strip().split('\n')

    current_section = []
    current_length = 0
    max_length = 1800

    for line in lines:
        line_len = len(line) + 1  # +1 for newline

        if current_length + line_len > max_length and current_section:
            # Save current section
            messages.append('\n'.join(current_section))
            current_section = []
            current_length = 0

        current_section.append(line)
        current_length += line_len

    # Add remaining
    if current_section:
        messages.append('\n'.join(current_section))

    return messages


def post_to_discord(channel_id: str, messages: list[str]) -> dict:
    """
    Post messages to Discord via the message tool.
    This is a stub that returns what the cron job needs to know.
    """
    return {
        "status": "ok",
        "posted_messages": len(messages),
        "total_chars": sum(len(m) for m in messages),
        "channel_id": channel_id,
        "messages": messages
    }


def main():
    # Read the latest markdown report
    report_path = ROOT / ".openclaw/reports/activity-briefing/latest.md"

    if not report_path.exists():
        print(json.dumps({
            "status": "error",
            "error": f"Report file not found: {report_path}"
        }))
        return 1

    content = report_path.read_text(encoding='utf-8')

    # Split into Discord-safe messages
    messages = split_activity_report(content)

    if not messages:
        print(json.dumps({
            "status": "error",
            "error": "Report is empty"
        }))
        return 1

    # In production, this would actually post via the message tool
    # For now, return the split messages
    result = {
        "status": "ok",
        "split_messages": len(messages),
        "message_lengths": [len(m) for m in messages],
        "first_message": messages[0][:200] + "..." if messages[0] else "",
        "ready_for_discord_post": True
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
