import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_production_readiness import build_assistant_production_readiness


def _scheduler(status="healthy", *, blocked_job=None):
    jobs = []
    for name in [
        "betty_mail_triage",
        "training_calendar_booking",
        "email_triage_booking",
        "task_spine",
        "coding_work_briefing",
        "task_command_capture",
        "daily_personal_briefing",
        "assistant_learning",
        "weekly_personal_review",
        "meeting_prep_briefing",
        "meeting_outcome_capture",
        "assistant_incident_manager",
    ]:
        job_status = "blocked" if name == blocked_job else "healthy"
        jobs.append({"job_name": name, "status": job_status, "failure_class": "launchagent_not_loaded" if job_status == "blocked" else None})
    return {"status": status if not blocked_job else "blocked", "jobs": jobs, "calendar_write_attempted": False}


def _ready(status="ready_verified"):
    return {"status": status, "production_ready": status in {"ready_verified", "calibration_pending"}, "reason": status, "calendar_write_attempted": False, "notion_write_attempted": False}


def test_ready_verified_when_all_lanes_are_clean():
    payload = build_assistant_production_readiness(
        expected_date="2026-05-25",
        expected_week="2026-W22",
        scheduler_health_payload=_scheduler(),
        daily_readiness_payload=_ready(),
        weekly_readiness_payload=_ready(),
        learning_readiness_payload=_ready("calibration_pending"),
        meeting_prep_readiness_payload=_ready(),
        meeting_outcome_readiness_payload=_ready(),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={"status": "ok", "issue_count": 0, "calendar_write_attempted": False, "notion_write_attempted": False},
        dead_letters=[],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "ready_verified"
    assert payload["production_ready"] is True
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False


def test_pending_natural_runs_are_reported_without_failing():
    payload = build_assistant_production_readiness(
        expected_date="2026-05-25",
        expected_week="2026-W22",
        scheduler_health_payload=_scheduler(),
        daily_readiness_payload=_ready("ready_pending_natural_run"),
        weekly_readiness_payload=_ready("ready_pending_natural_run"),
        learning_readiness_payload=_ready("ready_pending_natural_run"),
        meeting_prep_readiness_payload=_ready("ready_pending_natural_run"),
        meeting_outcome_readiness_payload=_ready("ready_pending_natural_run"),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={"status": "ok", "issue_count": 0, "calendar_write_attempted": False, "notion_write_attempted": False},
        dead_letters=[],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "ready_pending_natural_runs"
    assert "daily_personal_briefing" in payload["pending_gates"]
    assert "weekly_personal_review" in payload["pending_gates"]
    assert "meeting_prep_briefing" in payload["pending_gates"]
    assert "meeting_outcome_capture" in payload["pending_gates"]


def test_pending_natural_runs_take_precedence_over_hygiene_manual_review():
    payload = build_assistant_production_readiness(
        expected_date="2026-05-25",
        expected_week="2026-W22",
        scheduler_health_payload=_scheduler(),
        daily_readiness_payload=_ready("ready_pending_natural_run"),
        weekly_readiness_payload=_ready("ready_pending_natural_run"),
        learning_readiness_payload=_ready("ready_pending_natural_run"),
        meeting_prep_readiness_payload=_ready("ready_pending_natural_run"),
        meeting_outcome_readiness_payload=_ready("ready_pending_natural_run"),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={
            "status": "manual_review_required",
            "issue_count": 1,
            "orphan_rocky_events": [{"summary": "Rocky: Task focus - Reply to investor", "idempotency_key": "rocky:task_focus:test"}],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        },
        dead_letters=[],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "ready_pending_natural_runs"
    assert payload["manual_review_items"][0]["source"] == "calendar_hygiene"


def test_open_dead_letters_and_orphan_blocks_need_manual_review():
    payload = build_assistant_production_readiness(
        scheduler_health_payload=_scheduler(),
        daily_readiness_payload=_ready(),
        weekly_readiness_payload=_ready(),
        learning_readiness_payload=_ready("calibration_pending"),
        meeting_prep_readiness_payload=_ready(),
        meeting_outcome_readiness_payload=_ready(),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={
            "status": "manual_review_required",
            "issue_count": 1,
            "orphan_rocky_events": [{"summary": "Rocky: Task focus - Reply to investor", "idempotency_key": "rocky:task_focus:2026-05-25:885c3df38c2e8c65"}],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        },
        dead_letters=[{"dead_letter_id": "dead:1", "job_name": "training_calendar_booking", "failure_class": "x"}],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "manual_review_required"
    assert any(item["source"] == "assistant_dead_letters" for item in payload["manual_review_items"])
    assert any(item["source"] == "calendar_hygiene" for item in payload["manual_review_items"])


def test_communicated_business_block_is_manual_review_not_not_ready():
    scheduler = _scheduler()
    for job in scheduler["jobs"]:
        if job["job_name"] == "email_triage_booking":
            job["status"] = "healthy_with_user_action_required"
            job["failure_class"] = "email_triage_no_available_slot"

    payload = build_assistant_production_readiness(
        scheduler_health_payload=scheduler,
        daily_readiness_payload=_ready(),
        weekly_readiness_payload=_ready(),
        learning_readiness_payload=_ready("calibration_pending"),
        meeting_prep_readiness_payload=_ready(),
        meeting_outcome_readiness_payload=_ready(),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={"status": "ok", "issue_count": 0, "calendar_write_attempted": False, "notion_write_attempted": False},
        dead_letters=[{"dead_letter_id": "dead:1", "job_name": "email_triage_booking", "failure_class": "no_available_slot", "status": "waiting_for_user"}],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "manual_review_required"
    assert payload["not_ready_items"] == []
    assert any(item["job_name"] == "email_triage_booking" for item in payload["manual_review_items"])


def test_blocked_scheduler_or_calendar_health_is_not_ready():
    payload = build_assistant_production_readiness(
        scheduler_health_payload=_scheduler(blocked_job="email_triage_booking"),
        daily_readiness_payload=_ready(),
        weekly_readiness_payload=_ready(),
        learning_readiness_payload=_ready("calibration_pending"),
        meeting_prep_readiness_payload=_ready(),
        meeting_outcome_readiness_payload=_ready(),
        calendar_write_health_payload={"status": "blocked", "blocked_checks": ["eventkit"], "calendar_write_attempted": False},
        calendar_hygiene_payload={"status": "ok", "issue_count": 0, "calendar_write_attempted": False, "notion_write_attempted": False},
        dead_letters=[],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    assert payload["status"] == "not_ready"
    assert "email_triage_booking" in str(payload["not_ready_items"])
    assert "calendar_write_health" in str(payload["not_ready_items"])


def test_readiness_output_redacts_sensitive_content():
    payload = build_assistant_production_readiness(
        scheduler_health_payload=_scheduler(),
        daily_readiness_payload={**_ready(), "summary": "secret token abc"},
        weekly_readiness_payload=_ready(),
        learning_readiness_payload=_ready("calibration_pending"),
        meeting_prep_readiness_payload=_ready(),
        meeting_outcome_readiness_payload=_ready(),
        calendar_write_health_payload={"status": "ok", "calendar_write_attempted": False},
        calendar_hygiene_payload={"status": "ok", "issue_count": 0, "calendar_write_attempted": False, "notion_write_attempted": False, "raw_description": "cookie abc"},
        dead_letters=[],
        agentmail_health_payload={"status": "ok"},
        notion_health_payload={"status": "ok"},
    )

    text = str(payload).lower()
    assert "token abc" not in text
    assert "cookie abc" not in text
