import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_scheduler_state import AssistantSchedulerState
from weekly_personal_review_scheduler import list_weekly_personal_review_runs, run_weekly_personal_review_scheduler


def _signals():
    return {"status": "ok", "week_label": "2026-W22", "calendar": {"days": []}, "tasks": {"top_tasks": []}, "coding": {"top_items": []}, "scheduler": {"states": {}, "problem_jobs": []}, "dead_letters": {"open_count": 0}, "calendar_hygiene": {"issue_count": 0}, "learning": {"status": "ok"}}


def _plan():
    return {"status": "ok", "week_label": "2026-W22", "do_first": [{"category": "calendar", "title": "Plan week"}], "calendar_hygiene": {"issue_count": 0, "summary": "clean"}, "learning_or_calibration": {"status": "ok"}}


def test_weekly_scheduler_skips_non_monday_without_notification_or_writes(tmp_path):
    result = run_weekly_personal_review_scheduler(planning_date="2026-05-24", live=True, notify=True, notification_dry_run=True, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json")
    assert result["status"] == "skipped_not_weekly_review_day"
    assert result["notification"]["status"] == "skipped"
    assert result["calendar_write_attempted"] is False
    assert result["notion_write_attempted"] is False


def test_weekly_scheduler_dry_run_notification_preserves_format_and_records_run(tmp_path):
    result = run_weekly_personal_review_scheduler(planning_date="2026-05-25", now_local="2026-05-25T07:15:00+02:00", live=True, notify=True, notification_dry_run=True, signals_payload=_signals(), plan_payload=_plan(), scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json")
    assert result["status"] == "ok"
    assert result["notification"]["status"] == "dry_run"
    assert "\nDo first\n" in result["review"]["discord_message"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["target_week"] == "2026-W22"
    recent = list_weekly_personal_review_runs(limit=5, scheduler_db_path=tmp_path / "scheduler.sqlite3")
    assert recent["runs"][0]["target_week"] == "2026-W22"


def test_weekly_scheduler_notification_failure_dead_letters_without_calendar_write(tmp_path):
    def fail_post(**kwargs):
        return {"status": "failed", "reason": "discord_http_500"}
    result = run_weekly_personal_review_scheduler(planning_date="2026-05-25", now_local="2026-05-25T07:15:00+02:00", live=True, notify=True, post_func=fail_post, signals_payload=_signals(), plan_payload=_plan(), scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json")
    assert result["status"] == "degraded"
    assert result["dead_letter"]["job_name"] == "weekly_personal_review"
    assert result["calendar_write_attempted"] is False
    assert result["notion_write_attempted"] is False


def test_weekly_scheduler_duplicate_lock_records_recent_run(tmp_path):
    db = tmp_path / "scheduler.sqlite3"
    result = run_weekly_personal_review_scheduler(planning_date="2026-05-25", now_local="2026-05-25T07:15:00+02:00", live=False, notify=False, signals_payload=_signals(), plan_payload=_plan(), scheduler_db_path=db, ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", write_audit=False)
    with patch("weekly_personal_review_scheduler.acquire_run_lock", return_value=type("Lock", (), {"acquired": False, "reason": "active_lock_exists", "to_dict": lambda self: {"status": "duplicate_blocked"}})()):
        duplicate = run_weekly_personal_review_scheduler(planning_date="2026-05-25", now_local="2026-05-25T07:16:00+02:00", live=False, notify=False, signals_payload=_signals(), plan_payload=_plan(), scheduler_db_path=db, ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", write_audit=False)
    assert result["status"] == "ok"
    assert duplicate["status"] == "skipped_duplicate_run"
    recent = list_weekly_personal_review_runs(limit=5, scheduler_db_path=db)
    assert [run["status"] for run in recent["runs"][:2]] == ["skipped_duplicate_run", "ok"]
