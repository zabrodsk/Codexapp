import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_prep_briefing_builder import build_meeting_prep_briefing


def test_brief_includes_useful_focus_and_sanitized_refs():
    meeting = {"meeting_key": "meeting:1", "title": "Jana portfolio update", "start_local": "2026-05-25 10:00:00", "participant_count": 2}
    context = {
        "status": "ok",
        "confidence": 0.8,
        "source_refs": ["people/jana.md", "task:1"],
        "memory": {"items": [{"title": "Jana", "summary": "Prior context about portfolio."}]},
        "notion_tasks": {"items": [{"title": "Follow up with Jana", "priority": "High"}]},
        "email_context": {"items": [{"sender_domain": "example.com", "subject_preview": "Portfolio update"}]},
        "discord_context_notes": [{"preview": "Ask about runway."}],
    }

    payload = build_meeting_prep_briefing(meeting, context)

    assert payload["status"] == "ok"
    message = payload["discord_message"]
    assert "Rocky meeting prep" in message
    assert "Focus" in message
    assert "Dusan note" in message
    assert "Ask about runway" in message
    assert "token" not in message.lower()


def test_no_context_is_skipped():
    payload = build_meeting_prep_briefing({"meeting_key": "meeting:1"}, {"status": "skipped_no_context"})

    assert payload["status"] == "skipped_no_context"
    assert payload["calendar_write_attempted"] is False
