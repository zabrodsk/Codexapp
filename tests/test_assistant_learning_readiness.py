import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_readiness import evaluate_assistant_learning_readiness


def _health(*, natural_status="natural_run_verified", stderr_size=0, exit_code=0, helper=None):
    return {
        "status": "healthy",
        "signals": {
            "natural_run": {"status": natural_status},
            "launchagent": {"status": "healthy", "launchctl": {"loaded": True, "runs": 2, "last_exit_code": exit_code, "state": "not running"}},
            "logs": {"status": "healthy", "stderr_size": stderr_size, "stderr_hash": None},
            "helper_state": {"state": helper or {"last_run_at": "2026-05-25T18:46:00+00:00", "last_status": "calibration_pending", "target_date": "2026-05-25", "outcome_count": 12, "active_bounded_count": 0, "proposal_count": 3}},
        },
    }


def _summary(*, active=0, status="ok"):
    return {"status": status, "active_bounded_count": active, "proposal_count": 3, "outcome_count": 12, "preferences": [], "proposals": []}


def _run(status="succeeded"):
    return {"run_id": "run:test", "job_name": "assistant_learning", "status": status, "idempotency_key": "assistant-learning:2026-05-25", "created_at": "2026-05-25T18:46:00+00:00", "summary": "preference_models_updated"}


def test_learning_readiness_pending_before_natural_run_grace():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T21:00:00+02:00",
        health_payload=_health(natural_status="pending_first_weekday_run"),
        learning_summary_payload=_summary(status="empty"),
        recent_runs=[],
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "ready_pending_natural_run"
    assert payload["production_ready"] is False
    assert payload["calendar_write_attempted"] is False


def test_learning_readiness_calibration_pending_after_clean_run_without_active_preferences():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T23:00:00+02:00",
        health_payload=_health(),
        learning_summary_payload=_summary(active=0),
        recent_runs=[_run()],
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "calibration_pending"
    assert payload["production_ready"] is True
    assert payload["bounded_preferences_active"] is False
    assert "enough evidence" in payload["recovery_hints"][0]


def test_learning_readiness_ready_verified_with_active_bounded_preferences():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T23:00:00+02:00",
        health_payload=_health(helper={"last_run_at": "2026-05-25T18:46:00+00:00", "last_status": "ok", "target_date": "2026-05-25", "outcome_count": 12, "active_bounded_count": 1, "proposal_count": 3}),
        learning_summary_payload=_summary(active=1),
        recent_runs=[_run()],
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "ready_verified"
    assert payload["bounded_preferences_active"] is True


def test_learning_readiness_missing_store_after_grace_is_not_ready():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T23:00:00+02:00",
        health_payload=_health(),
        learning_summary_payload=_summary(status="empty"),
        recent_runs=[_run()],
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "not_ready"
    assert payload["reason"] == "assistant_learning_store_missing"


def test_learning_readiness_open_dead_letter_requires_manual_review():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T23:00:00+02:00",
        health_payload=_health(),
        learning_summary_payload=_summary(active=0),
        recent_runs=[_run()],
        dead_letters=[{"dead_letter_id": "dead:test", "job_name": "assistant_learning", "failure_class": "discord token secret failed", "safe_summary": "token secret body"}],
        audit_events=[],
    )

    assert payload["status"] == "manual_review_required"
    assert "token secret" not in str(payload).lower()


def test_learning_readiness_stderr_after_grace_is_not_ready():
    payload = evaluate_assistant_learning_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T23:00:00+02:00",
        health_payload=_health(stderr_size=42),
        learning_summary_payload=_summary(active=1),
        recent_runs=[_run()],
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "not_ready"
    assert payload["reason"] == "assistant_learning_stderr_not_empty"
