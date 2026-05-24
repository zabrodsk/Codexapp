import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_outcome_reader import collect_meeting_outcome_candidates


def test_reader_collects_structured_outcome_without_transcript_body(tmp_path):
    note = tmp_path / "2026-05-23-investor.md"
    note.write_text(
        """---
title: Jana investor update
date: 2026-05-23
meeting_key: meeting:jana
---
# Jana investor update

## Transcript
This raw transcript body should not be copied.

## Decisions
- We agreed to send the portfolio update.

## Follow-ups
- Dusan: send Jana the updated deck.
- Jana: share partner feedback.
""",
        encoding="utf-8",
    )

    payload = collect_meeting_outcome_candidates(meeting_dir=tmp_path, now=datetime(2026, 5, 24, tzinfo=timezone.utc))

    assert payload["status"] == "ok"
    assert payload["candidate_count"] == 1
    candidate = payload["candidates"][0]
    assert candidate["meeting_key"] == "meeting:jana"
    serialized = str(candidate)
    assert "raw transcript body" not in serialized
    assert "send Jana" in serialized


def test_reader_redacts_secret_like_text(tmp_path):
    note = tmp_path / "2026-05-23-secret.md"
    note.write_text(
        "# Secret meeting\n\n## Follow-ups\n- Dusan: open https://example.com/?token=abc and reply.\n",
        encoding="utf-8",
    )

    payload = collect_meeting_outcome_candidates(meeting_dir=tmp_path, now=datetime(2026, 5, 24, tzinfo=timezone.utc))

    assert "token=abc" not in str(payload)
    assert "[redacted]" in str(payload)
