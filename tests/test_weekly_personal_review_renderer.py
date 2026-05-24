import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from weekly_personal_review_renderer import render_weekly_personal_review


def test_weekly_renderer_preserves_sections_and_redacts_secrets():
    payload = render_weekly_personal_review(
        {"week_label": "2026-W22"},
        {
            "do_first": [{"category": "task", "title": "Follow up", "reason": "High", "source_ref": "url?token=secret"}],
            "calendar_hygiene": {"issue_count": 0, "summary": "clean"},
            "learning_or_calibration": {"status": "calibration_pending", "active_bounded_count": 0, "proposal_count": 1, "outcome_count": 5},
        },
    )
    message = payload["discord_message"]
    assert "Rocky weekly review - 2026-W22" in message
    assert "\nDo first\n" in message
    assert "\nCalendar hygiene\n" in message
    assert "secret" not in message.lower()
    assert "token" not in message.lower()
