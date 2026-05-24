import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from weekly_personal_signal_collector import collect_weekly_personal_signals


def test_weekly_collector_summarizes_all_lanes_without_mutation():
    payload = collect_weekly_personal_signals(
        planning_date="2026-05-25",
        calendar_events=[{"summary": "Rocky: Coding focus - A", "start_local": "2026-05-25 13:00:00", "end_local": "2026-05-25 14:00:00", "all_day": False, "calendar": "Calendar", "location": "", "description": "Booked by: Rocky\nIdempotency key: rocky:coding:1"}],
        scheduler_states={"training_calendar_booking": {"last_status": "skipped_duplicate", "target_date": "2026-05-27"}},
        tasks_payload={"status": "ok", "tasks": [{"title": "Follow up", "priority": "High", "page_id": "task-1", "source_ref": "email:1"}]},
        command_payload={"status": "ok", "commands": [{"source": "discord", "status": "applied", "preview": "remember follow up"}]},
        coding_payload={"status": "ok", "selected_focus_items": [{"project": "Rocky", "title": "Weekly review", "confidence": 0.9}]},
        learning_payload={"status": "ok", "active_bounded_count": 0, "proposal_count": 2, "outcome_count": 12},
        hygiene_payload={"status": "ok", "issue_count": 0},
    )

    assert payload["status"] == "ok"
    assert payload["week_label"] == "2026-W22"
    assert payload["calendar"]["rocky_block_count"] == 1
    assert payload["tasks"]["high_count"] == 1
    assert payload["command_activity"]["command_count"] == 1
    assert payload["learning"]["proposal_count"] == 2
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    assert "description" not in json.dumps(payload).lower()
