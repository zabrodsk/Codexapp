import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_scheduler_state import AssistantSchedulerState
from weekly_personal_review_readiness import evaluate_weekly_personal_review_readiness


def _health(tmp_path, *, state=None, stderr_size=0, loaded=True, status="healthy"):
    return {
        "status": status,
        "signals": {
            "launchagent": {"label": "com.openclaw.rocky-weekly-personal-review", "status": status, "launchctl": {"loaded": loaded, "runs": 1, "last_exit_code": 0}},
            "logs": {"stderr_size": stderr_size, "status": "healthy" if stderr_size == 0 else "blocked"},
            "helper_state": {"state": state or {}},
        },
    }


def test_weekly_readiness_pending_before_grace(tmp_path):
    payload = evaluate_weekly_personal_review_readiness(expected_week="2026-W22", now_local="2026-05-25T08:00:00+02:00", health_payload=_health(tmp_path), recent_runs=[], dead_letters=[])
    assert payload["status"] == "ready_pending_natural_run"


def test_weekly_readiness_verified_after_clean_run(tmp_path):
    db = tmp_path / "scheduler.sqlite3"
    state = AssistantSchedulerState(db)
    state.record_job_run(job_name="weekly_personal_review", status="ok", idempotency_key="weekly-personal-review:2026-W22", scheduled_for="2026-05-25", summary=json.dumps({"target_week": "2026-W22", "notification_status": "posted", "calendar_write_attempted": False, "notion_write_attempted": False}))
    helper_state = {"target_week": "2026-W22", "notification_status": "posted", "calendar_write_attempted": False, "notion_write_attempted": False}
    payload = evaluate_weekly_personal_review_readiness(expected_week="2026-W22", now_local="2026-05-25T10:00:00+02:00", scheduler_db_path=db, health_payload=_health(tmp_path, state=helper_state), dead_letters=[])
    assert payload["status"] == "ready_verified"


def test_weekly_readiness_manual_review_for_unexpected_side_effect(tmp_path):
    db = tmp_path / "scheduler.sqlite3"
    state = AssistantSchedulerState(db)
    state.record_job_run(job_name="weekly_personal_review", status="ok", idempotency_key="weekly-personal-review:2026-W22", scheduled_for="2026-05-25", summary=json.dumps({"target_week": "2026-W22", "notification_status": "posted", "calendar_write_attempted": True, "notion_write_attempted": False}))
    helper_state = {"target_week": "2026-W22", "notification_status": "posted", "calendar_write_attempted": True, "notion_write_attempted": False}
    payload = evaluate_weekly_personal_review_readiness(expected_week="2026-W22", now_local="2026-05-25T10:00:00+02:00", scheduler_db_path=db, health_payload=_health(tmp_path, state=helper_state), dead_letters=[])
    assert payload["status"] == "manual_review_required"
