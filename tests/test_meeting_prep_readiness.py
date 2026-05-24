import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_scheduler_state import AssistantSchedulerState
from meeting_prep_readiness import evaluate_meeting_prep_readiness


def test_readiness_pending_before_grace(tmp_path):
    payload = evaluate_meeting_prep_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T07:35:00+02:00",
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        state_file=tmp_path / "state.json",
        stderr_path=tmp_path / "err.log",
        launchagent_payload={"status": "healthy", "launchctl": {"runs": 0, "last_exit_code": 0}, "issues": []},
    )

    assert payload["status"] == "ready_pending_natural_run"
    assert payload["calendar_write_attempted"] is False


def test_readiness_verified_after_clean_run(tmp_path):
    state_db = tmp_path / "scheduler.sqlite3"
    AssistantSchedulerState(state_db).record_job_run(
        job_name="meeting_prep_briefing",
        status="skipped_no_due_meetings",
        idempotency_key="meeting-prep:2026-05-25:08:00",
        scheduled_for="2026-05-25",
        summary=json.dumps({"target_date": "2026-05-25", "status": "skipped_no_due_meetings"}),
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"target_date": "2026-05-25", "last_status": "skipped_no_due_meetings"}))
    stderr = tmp_path / "err.log"
    stderr.write_text("")

    payload = evaluate_meeting_prep_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T08:30:00+02:00",
        scheduler_db_path=state_db,
        state_file=state_file,
        stderr_path=stderr,
        launchagent_payload={"status": "healthy", "launchctl": {"runs": 1, "last_exit_code": 0}, "issues": []},
    )

    assert payload["status"] == "ready_verified"


def test_readiness_not_ready_when_run_missing_after_grace(tmp_path):
    payload = evaluate_meeting_prep_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T08:30:00+02:00",
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        state_file=tmp_path / "state.json",
        stderr_path=tmp_path / "err.log",
        launchagent_payload={"status": "healthy", "launchctl": {"runs": 0, "last_exit_code": 0}, "issues": []},
    )

    assert payload["status"] == "not_ready"
    assert payload["issues"][0]["reason"] == "meeting_prep_run_missing_after_grace"
