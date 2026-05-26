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
    assert "Best read" in message
    assert "Your latest note" in message
    assert "Ask about runway" in message
    assert "token" not in message.lower()


def test_no_context_is_skipped():
    payload = build_meeting_prep_briefing({"meeting_key": "meeting:1"}, {"status": "skipped_no_context"})

    assert payload["status"] == "skipped_no_context"
    assert payload["calendar_write_attempted"] is False


def test_brief_is_human_readable_and_asks_for_guidance_when_context_is_partial():
    meeting = {
        "meeting_key": "meeting:flo",
        "title": "FLO & RockawayQ",
        "start_local": "2026-05-26 13:30:00",
        "participant_count": 5,
    }
    context = {
        "status": "ok",
        "confidence": 0.66,
        "source_refs": [
            "openclaw-memory/companies/flo.md",
            "apple-mail:message:13f325ad84d5bdb3",
        ],
        "relevance": {
            "topic_terms": ["flo", "rockawayq", "andrej"],
            "clarification_needed": True,
            "dropped_counts": {"notion_tasks": 3},
        },
        "memory": {
            "items": [
                {
                    "title": "Flo / FLO Group",
                    "summary": "@@ -23,4 @@ (22 before, 1 after) - **Active acquisition pipeline:** NextLevel and LeverUp commercial DD.",
                }
            ]
        },
        "notion_tasks": {"items": []},
        "email_context": {"items": [{"sender_domain": "rockaway.cz", "subject_preview": "[focus] FLO Spin-off < commercial setup >"}]},
        "discord_context_notes": [],
    }

    payload = build_meeting_prep_briefing(meeting, context)
    message = payload["discord_message"]

    assert "I may need your steer" in message
    assert "reply in Discord" in message
    assert "not enough to know whether" in message
    assert "FLO Spin-off" in message
    assert "@@" not in message
    assert "dedupe_key" not in message
    assert " From:" not in message
    assert "RockawayQ / MTX press-release" not in message
