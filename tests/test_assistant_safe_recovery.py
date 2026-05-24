import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState
from assistant_scheduler_state import AssistantSchedulerState
from assistant_safe_recovery import build_safe_recovery_candidates, run_safe_recovery_action


def test_recovery_candidates_are_report_only_and_sanitized(tmp_path):
    db = tmp_path / "scheduler.sqlite3"
    state = AssistantSchedulerState(db)
    dead = state.upsert_dead_letter(
        job_name="training_calendar_booking",
        workflow="training_calendar_scheduler",
        idempotency_key="rocky:training:test",
        failure_class="calendar_write_failed",
        safe_summary="token secret failed",
        recovery_hint="inspect",
    )

    payload = build_safe_recovery_candidates(
        scheduler_db_path=db,
        calendar_hygiene_payload={
            "status": "manual_review_required",
            "stale_state_candidates": [{"idempotency_key": "rocky:task:test", "title": "Rocky: Task focus"}],
            "orphan_rocky_events": [{"idempotency_key": "rocky:orphan:test", "summary": "Rocky: Task focus"}],
        },
    )

    assert payload["status"] == "manual_review_required"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    assert any(item["kind"] == "dead_letter" and item["dead_letter_id"] == dead["dead_letter_id"] for item in payload["candidates"])
    assert "token secret" not in str(payload).lower()


def test_recovery_refuses_mutation_without_live(tmp_path):
    payload = run_safe_recovery_action(
        action="update-dead-letter",
        dead_letter_id="dead:missing",
        status="recovered",
        live=False,
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["state_mutated"] is False


def test_mark_calendar_stale_updates_state_only_when_candidate(tmp_path):
    state_db = tmp_path / "calendar.sqlite3"
    state = AssistantCalendarState(state_db)
    state.record_created(
        idempotency_key="rocky:task:test",
        calendar_name="Calendar",
        title="Rocky: Task focus - Test",
        start="2026-05-25T13:00:00+02:00",
        end="2026-05-25T14:00:00+02:00",
        event_uid=None,
        create_audit_id="audit-create",
    )
    ledger = tmp_path / "audit.jsonl"

    with patch("assistant_safe_recovery.inspect_calendar_block", return_value={"status": "stale_state_candidate", "idempotency_key": "rocky:task:test"}):
        payload = run_safe_recovery_action(
            action="mark-calendar-stale",
            idempotency_key="rocky:task:test",
            live=True,
            calendar_state_db_path=state_db,
            audit_log_path=ledger,
        )

    assert payload["status"] == "recovered"
    assert payload["state_mutated"] is True
    assert payload["calendar_write_attempted"] is False
    assert state.get("rocky:task:test")["status"] == "stale"
    assert "calendar.state_marked_stale" in ledger.read_text()


def test_update_dead_letter_status_is_state_only_and_audited(tmp_path):
    scheduler_db = tmp_path / "scheduler.sqlite3"
    state = AssistantSchedulerState(scheduler_db)
    dead = state.upsert_dead_letter(
        job_name="email_triage_booking",
        workflow="email_triage_scheduler",
        idempotency_key="email:test",
        failure_class="mail_read_failed",
        safe_summary="Mail read failed",
    )
    ledger = tmp_path / "audit.jsonl"

    payload = run_safe_recovery_action(
        action="update-dead-letter",
        dead_letter_id=dead["dead_letter_id"],
        status="recovered",
        live=True,
        scheduler_db_path=scheduler_db,
        audit_log_path=ledger,
    )

    assert payload["status"] == "recovered"
    assert payload["state"]["status"] == "recovered"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    event = json.loads(ledger.read_text().strip())
    assert event["event_type"] == "scheduler.dead_letter_resolved"
