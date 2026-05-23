import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_work_scheduler import run_coding_work_scheduler


def _item():
    return {
        "work_item_id": "coding-work:1",
        "project": "Matchbook",
        "title": "Finish go-live smoke",
        "status": "active",
        "priority": "High",
        "estimated_effort_minutes": 90,
        "confidence": 0.86,
        "requires_dusan_decision": False,
        "source_refs": ["codex:session:1"],
        "where_left_off": "Tests passed",
        "recommended_next_step": "Run smoke",
    }


def test_weekend_scheduler_skips_without_calendar_write(tmp_path):
    payload = run_coding_work_scheduler(planning_date="2026-05-23", live=True, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json")

    assert payload["status"] == "skipped_weekend_target"
    assert payload["calendar_write_attempted"] is False


def test_live_scheduler_books_one_eligible_focus_block(tmp_path):
    with patch("coding_work_scheduler.calendar_write_health", return_value={"status": "ok"}), patch("coding_work_scheduler.book_coding_focus_proposal", return_value={"status": "created", "calendar_write_attempted": True, "calendar_event_created": True}) as booker:
        payload = run_coding_work_scheduler(planning_date="2026-05-25", live=True, notify=True, notification_dry_run=True, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", work_items=[_item()], existing_events=[], llm_func=lambda prompt: {"status": "ok", "provider": "test", "model": "test", "text": '{"items": []}'})

    assert payload["status"] == "ok"
    assert payload["created_count"] == 1
    assert booker.call_count == 1
    assert payload["notification"]["status"] == "dry_run"


def test_calendar_health_failure_creates_dead_letter(tmp_path):
    with patch("coding_work_scheduler.calendar_write_health", return_value={"status": "blocked", "reason": "tcc"}):
        payload = run_coding_work_scheduler(planning_date="2026-05-25", live=True, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", work_items=[_item()], existing_events=[], llm_func=lambda prompt: {"status": "ok", "provider": "test", "model": "test", "text": '{"items": []}'})

    assert payload["status"] == "blocked"
    assert payload["dead_letter"]["failure_class"] == "calendar_write_health_blocked"
