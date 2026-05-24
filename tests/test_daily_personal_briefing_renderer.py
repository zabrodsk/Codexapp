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
