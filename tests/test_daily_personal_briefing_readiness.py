import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_personal_briefing_readiness import evaluate_daily_personal_briefing_readiness


def _health(*, natural="natural_run_verified", notification="posted", stderr_size=0, exit_code=0, target_date="2026-05-25", last_run_at="2026-05-25T09:36:00+00:00", status="healthy", booking_results=None):
    return {
        "status": status,
        "failure_class": None if status == "healthy" else "daily_personal_briefing_natural_run_failed",
        "signals": {
            "natural_run": {
                "status": natural,
                "expected_run": "2026-05-25T11:35:00+02:00",
                "grace_until": "2026-05-25T13:35:00+02:00",
            },
            "launchagent": {
                "label": "com.openclaw.rocky-daily-personal-briefing",
                "status": "healthy",
                "launchctl": {"loaded": True, "runs": 2, "last_exit_code": exit_code, "state": "not running"},
            },
            "logs": {
                "stdout_path": "/Users/clawdbot/.openclaw/logs/rocky-daily-personal-briefing.log",
                "stderr_path": "/Users/clawdbot/.openclaw/logs/rocky-daily-personal-briefing.err.log",
                "stderr_size": stderr_size,
                "stderr_hash": "errhash" if stderr_size else None,
                "status": "blocked" if stderr_size else "healthy",
            },
            "helper_state": {
                "status": "ok",
                "state": {
                    "last_run_at": last_run_at,
                    "last_status": "ok" if status == "healthy" else "failed",
                    "target_date": target_date,
                    "reason": "daily_personal_briefing_completed",
                    "notification_status": notification,
                    "created_count": 0,
                    "skipped_count": 0,
                    "booking_results": booking_results or [],
                    "error_hash": None,
                },
            },
        },
    }


def _recent(*, notification="posted", status="ok", target_date="2026-05-25", created_count=0):
    return {
        "status": "ok",
        "count": 1,
        "runs": [
            {
                "run_id": "run:daily",
                "status": status,
                "target_date": target_date,
                "reason": "daily_personal_briefing_completed",
                "notification_status": notification,
                "safe_booking_mode": "live",
                "created_count": created_count,
                "skipped_count": 0,
                "message_sha256": "msgsha",
                "idempotency_key": "daily-personal-briefing:2026-05-25",
                "created_at": "2026-05-25T09:36:01+00:00",
                "updated_at": "2026-05-25T09:36:01+00:00",
            }
        ],
    }


def test_readiness_pending_before_grace_window():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T12:00:00+02:00",
        health_payload=_health(natural="pending_first_weekday_run", notification="dry_run"),
        recent_payload=_recent(notification="dry_run"),
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "ready_pending_natural_run"
    assert payload["production_ready"] is False
    assert payload["calendar_write_attempted"] is False


def test_readiness_successful_natural_run_is_verified():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(),
        recent_payload=_recent(),
        dead_letters=[],
        audit_events=[{"audit_id": "audit-1", "event_type": "scheduler.health_ok", "created_at": "2026-05-25T09:40:00+00:00", "decision": "allowed"}],
    )

    assert payload["status"] == "ready_verified"
    assert payload["production_ready"] is True
    assert payload["evidence"]["launchagent"]["last_exit_code"] == 0


def test_readiness_missed_natural_run_after_grace_is_not_ready():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(natural="natural_run_failed", notification="skipped", target_date="2026-05-24", last_run_at="2026-05-24T08:00:00+00:00", status="degraded"),
        recent_payload={"status": "ok", "count": 0, "runs": []},
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "not_ready"
    assert payload["reason"] == "daily_briefing_natural_run_not_verified"


def test_readiness_nonzero_launchagent_exit_or_stderr_is_not_ready():
    nonzero = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(exit_code=1),
        recent_payload=_recent(),
        dead_letters=[],
        audit_events=[],
    )
    stderr = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(stderr_size=128),
        recent_payload=_recent(),
        dead_letters=[],
        audit_events=[],
    )

    assert nonzero["status"] == "not_ready"
    assert stderr["status"] == "not_ready"
    assert stderr["reason"] == "daily_briefing_stderr_not_empty"


def test_readiness_discord_failure_with_dead_letter_requires_manual_review():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(notification="failed"),
        recent_payload=_recent(notification="failed"),
        dead_letters=[{"dead_letter_id": "dead-1", "failure_class": "assistant.notification_failed", "safe_summary": "Discord failed", "recovery_hint": "Inspect notification", "last_failed_at": "2026-05-25T09:36:00+00:00", "attempts": 1}],
        audit_events=[],
    )

    assert payload["status"] == "manual_review_required"
    assert payload["reason"] == "daily_briefing_notification_failed_with_dead_letter"


def test_readiness_unexpected_calendar_write_requires_manual_review():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",
        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(booking_results=[{"action": "raw_calendar_write", "status": "created", "calendar_write_attempted": True}]),
        recent_payload=_recent(created_count=1),
        dead_letters=[],
        audit_events=[],
    )

    assert payload["status"] == "manual_review_required"
    assert payload["reason"] == "daily_briefing_unexpected_calendar_side_effect"


def test_readiness_output_redacts_secrets():
    payload = evaluate_daily_personal_briefing_readiness(
        expected_date="2026-05-25",

        now_local="2026-05-25T14:00:00+02:00",
        health_payload=_health(),
        recent_payload={"status": "ok", "count": 1, "runs": [{**_recent()["runs"][0], "top_priority": {"title": "https://example.com/?token=abc123"}}]},
        dead_letters=[{"dead_letter_id": "dead-1", "failure_class": "x", "safe_summary": "secret token abc123", "recovery_hint": "cookie abc", "last_failed_at": "now", "attempts": 1}],
        audit_events=[],
    )

    text = str(payload)
    assert "abc123" not in text
    assert "cookie abc" not in text
