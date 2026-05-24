import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_personal_briefing_scheduler import run_daily_personal_briefing_scheduler


def _signals():
    return {
        "status": "ok",
        "planning_date": "2026-05-25",
        "booking_allowed_today": True,
        "calendar": {"event_count": 1, "free_windows": [{"start": "12:30", "end": "14:30", "minutes": 120}]},
        "training": {"today_block_count": 1, "summary": "Training protected."},
        "email": {"status": "proposal", "attention_count": 3, "estimated_minutes": 45, "repair_candidate": True, "idempotency_key": "rocky:email:1"},
        "tasks": {"urgent_count": 0, "due_soon_count": 0, "top_tasks": []},
        "coding": {"selected_count": 1, "top_items": [{"project": "Rocky", "title": "Daily brief", "confidence": 0.86, "priority": "High"}], "proposal_idempotency_keys": ["rocky:coding:1"], "selected_focus_items": [{"project": "Rocky", "title": "Daily brief", "confidence": 0.86}]},
        "task_focus": {"status": "skipped_no_focus_tasks"},
        "dead_letters": {"open_count": 0, "items": []},
        "scheduler": {"problem_count": 0, "states": {}},
    }


def test_weekend_scheduler_skips_without_notification_or_calendar_write(tmp_path):
    result = run_daily_personal_briefing_scheduler(
        planning_date="2026-05-24",
        live=True,
        notify=True,
        notification_dry_run=True,
        apply_safe_bookings=True,
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        ledger_path=tmp_path / "audit.jsonl",
        state_file=tmp_path / "state.json",
        write_audit=True,
    )

    assert result["status"] == "skipped_weekend_briefing"
    assert result["calendar_write_attempted"] is False
    assert result["notification"]["status"] == "skipped"


def test_scheduler_applies_email_repair_through_existing_lane_only_for_real_today(tmp_path):
    with patch("daily_personal_briefing_scheduler.run_email_triage_scheduler", return_value={"status": "created", "calendar_write_attempted": True, "calendar_event_created": True}) as email_run:
        result = run_daily_personal_briefing_scheduler(
            planning_date="2026-05-25",
            now_local="2026-05-25T11:35:00+02:00",
            live=True,
            notify=True,
            notification_dry_run=True,
            apply_safe_bookings=True,
            signals_payload=_signals(),
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state.json",
            write_audit=True,
        )

    assert result["status"] == "ok"
    assert result["calendar_write_attempted"] is True
    assert result["booking_results"][0]["action"] == "email_triage_repair"
    email_run.assert_called_once()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["last_status"] == "ok"


def test_scheduler_does_not_live_book_for_fixture_date_that_is_not_today(tmp_path):
    with patch("daily_personal_briefing_scheduler.run_email_triage_scheduler") as email_run:
        result = run_daily_personal_briefing_scheduler(
            planning_date="2026-05-25",
            now_local="2026-05-26T11:35:00+02:00",
            live=True,
            notify=True,
            notification_dry_run=True,
            apply_safe_bookings=True,
            signals_payload=_signals(),
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state.json",
            write_audit=True,
        )

    assert result["calendar_write_attempted"] is False
    assert result["booking_results"] == []
    email_run.assert_not_called()
    assert result["safe_booking_mode"] == "dry_run_not_today"
