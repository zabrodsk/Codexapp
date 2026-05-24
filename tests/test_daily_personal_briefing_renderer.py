import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_personal_briefing_renderer import render_daily_personal_briefing


def test_renders_concise_sanitized_discord_briefing():
    payload = render_daily_personal_briefing(
        {
            "planning_date": "2026-05-25",
            "calendar": {"event_count": 3, "free_windows": [{"start": "12:30", "end": "14:00", "minutes": 90}]},
            "training": {"summary": "Training protected 08:00-10:30."},
        },
        {
            "top_priority": {"category": "coding", "title": "Finish Rocky daily brief"},
            "do_first": [{"category": "coding", "title": "Finish Rocky daily brief", "reason": "high confidence"}],
            "protected_time": ["Training protected 08:00-10:30."],
            "needs_decision": [{"category": "task", "title": "Choose launch timing"}],
            "blocked_or_risky": [{"category": "scheduler", "title": "email_triage_booking", "reason": "failed token secret"}],
            "suggested_focus": [{"category": "coding", "title": "Rocky", "next_step": "Open the briefing scheduler"}],
            "what_rocky_handled": ["Email triage block already exists."],
            "safe_booking_actions": [{"action": "coding_focus_book", "idempotency_key": "rocky:coding:1"}],
        },
    )

    assert payload["status"] == "ok"
    message = payload["discord_message"]
    assert "Rocky daily brief - 2026-05-25" in message
    for section in ["Today", "Do first", "Protected time", "Needs decision", "Blocked or risky", "Suggested focus", "What Rocky handled"]:
        assert section in message
    assert "token" not in message.lower()
    assert "secret" not in message.lower()
    assert len(message) < 1900


def test_discord_briefing_preserves_section_line_breaks():
    payload = render_daily_personal_briefing(
        {
            "planning_date": "2026-05-25",
            "calendar": {"event_count": 1, "free_windows": []},
        },
        {
            "top_priority": {"category": "task", "title": "Review Monday plan"},
            "do_first": [{"category": "task", "title": "Review Monday plan", "reason": "High"}],
            "deferred_or_did_not_fit": [{"category": "coding", "title": "Rocky", "reason": "Urgent task took priority"}],
        },
    )

    message = payload["discord_message"]
    assert "\nToday\n" in message
    assert "\nDo first\n" in message
    assert "\nDeferred / did not fit\n" in message
    assert "Rocky daily brief - 2026-05-25\n\nToday\n" in message


def test_daily_briefing_renders_learning_summary():
    payload = render_daily_personal_briefing(
        {"planning_date": "2026-05-25", "calendar": {"event_count": 0, "free_windows": []}, "learning": {"status": "ok", "active_bounded_count": 1, "proposal_count": 2, "outcome_count": 7}},
        {"top_priority": {"category": "task", "title": "Focus"}, "do_first": [], "protected_time": [], "needs_decision": [], "blocked_or_risky": [], "suggested_focus": [], "deferred_or_did_not_fit": [], "what_rocky_handled": []},
    )

    assert "Rocky is learning" in payload["discord_message"]
    assert "1 bounded preference" in payload["discord_message"]
