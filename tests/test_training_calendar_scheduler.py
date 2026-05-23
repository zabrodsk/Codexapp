import json
from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState
from assistant_run_lock import acquire_run_lock
from assistant_scheduler_state import AssistantSchedulerState
from training_calendar_scheduler import (
    WORKFLOW,
    run_training_calendar_scheduler,
    scheduler_idempotency_key,
)


def _workout(**overrides):
    payload = {
        "source": "trainingpeaks_webcal",
        "source_ref": "trainingpeaks:test",
        "date": "2026-05-27",
        "planned_start_local": None,
        "planned_end_local": None,
        "planned_duration_minutes": None,
        "title": "Run: Recovery Run 60 min",
        "sport": "run",
        "confidence": "low",
        "warnings": ["untimed_or_all_day_workout"],
        "observed_at": "2026-05-23T12:00:00+02:00",
    }
    payload.update(overrides)
    return payload


def _preview(workouts):
    return {
        "status": "ok",
        "source": "trainingpeaks_webcal",
        "workout_count": len(workouts),
        "workouts": workouts,
        "warnings": [],
        "calendar_write_attempted": False,
    }


def _ok_health():
    return {"status": "ok", "blocked_checks": [], "calendar_write_attempted": False}


def _paths(tmp_path):
    return {
        "scheduler_db_path": tmp_path / "assistant_scheduler.sqlite3",
        "calendar_state_db_path": tmp_path / "assistant_calendar.sqlite3",
        "ledger_path": tmp_path / "assistant_audit.jsonl",
        "state_file": tmp_path / "training_calendar_scheduler.json",
    }


def test_missing_live_is_dry_run_and_does_not_call_booking(tmp_path):
    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            health_payload=_ok_health(),
            live=False,
            **_paths(tmp_path),
        )

    assert payload["status"] == "dry_run_proposal"
    assert payload["calendar_write_attempted"] is False
    assert payload["selected_proposal"]["idempotency_key"]
    assert (tmp_path / "training_calendar_scheduler.json").exists()
    book.assert_not_called()


def test_duplicate_run_lock_prevents_overlap(tmp_path):
    run_key = scheduler_idempotency_key(
        planning_day=date(2026, 5, 23),
        target_day=date(2026, 5, 27),
    )
    acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=600,
        db_path=tmp_path / "assistant_scheduler.sqlite3",
        write_audit=False,
    )

    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["status"] == "skipped_duplicate_run"
    assert payload["calendar_write_attempted"] is False
    book.assert_not_called()


def test_friday_target_workout_is_cleanly_skipped_before_booking(tmp_path):
    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-26",
            preview_payload=_preview([_workout(date="2026-05-29")]),
            existing_events=[],
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["target_date"] == "2026-05-29"
    assert payload["status"] == "skipped_weekend_target"
    assert payload["calendar_write_attempted"] is False
    book.assert_not_called()


def test_friday_run_may_book_future_wednesday_target(tmp_path):
    with patch(
        "training_calendar_scheduler.book_training_calendar_proposal",
        return_value={
            "status": "created",
            "reason": None,
            "idempotency_key": "rocky:training:2026-05-27:test",
            "audit_id": "audit-created",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-22",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["target_date"] == "2026-05-27"
    assert payload["status"] == "created"
    assert payload["calendar_write_attempted"] is True
    book.assert_called_once()


def test_no_workout_is_clean_skip(tmp_path):
    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=_preview([]),
            existing_events=[],
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["status"] == "skipped_no_workout"
    assert payload["reason"] == "no_trainingpeaks_workout_on_target_date"
    book.assert_not_called()


def test_existing_active_duplicate_is_successful_without_writer(tmp_path):
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    preview = _preview([_workout()])
    existing = [
        {
            "summary": "Rocky: Training - Run: Recovery Run 60 min",
            "description": "Booked by: Rocky",
            "start_local": "2026-05-27 08:00:00",
            "end_local": "2026-05-27 10:30:00",
            "all_day": False,
            "calendar": "Calendar",
        }
    ]
    state.record_created(
        idempotency_key="rocky:training:2026-05-27:test",
        calendar_name="Calendar",
        title="Rocky: Training - Run: Recovery Run 60 min",
        start="2026-05-27T08:00:00+02:00",
        end="2026-05-27T10:30:00+02:00",
        event_uid="uid-existing",
        create_audit_id="audit-existing",
    )
    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=preview,
            existing_events=existing,
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["status"] == "skipped_duplicate"
    assert payload["reason"] == "duplicate_rocky_block"
    assert payload["calendar_write_attempted"] is False
    book.assert_not_called()


def test_multiple_eligible_proposals_blocks_and_dead_letters(tmp_path):
    payload = run_training_calendar_scheduler(
        planning_date="2026-05-23",
        preview_payload=_preview([
            _workout(source_ref="trainingpeaks:one"),
            _workout(source_ref="trainingpeaks:two", title="Bike: Endurance 90 min", sport="bike"),
        ]),
        existing_events=[],
        health_payload=_ok_health(),
        live=True,
        **_paths(tmp_path),
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "manual_review_required_multiple_training_proposals"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "manual_review_required_multiple_training_proposals"


def test_calendar_health_failure_blocks_before_booking_and_dead_letters(tmp_path):
    with patch("training_calendar_scheduler.book_training_calendar_proposal") as book:
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            health_payload={"status": "blocked", "blocked_checks": ["eventkit_authorization"]},
            live=True,
            **_paths(tmp_path),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "calendar_write_health_not_ok"
    book.assert_not_called()
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "calendar_write_health_not_ok"


def test_writer_failure_dead_letters(tmp_path):
    with patch(
        "training_calendar_scheduler.book_training_calendar_proposal",
        return_value={
            "status": "failed",
            "reason": "osascript_create_failed",
            "idempotency_key": "rocky:training:2026-05-27:test",
            "audit_id": "audit-failed",
            "calendar_write_attempted": True,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        },
    ):
        payload = run_training_calendar_scheduler(
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            health_payload=_ok_health(),
            live=True,
            **_paths(tmp_path),
        )

    assert payload["status"] == "failed"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "osascript_create_failed"


def test_output_state_and_audit_redact_auth_like_strings(tmp_path):
    payload = run_training_calendar_scheduler(
        planning_date="2026-05-23",
        preview_payload=_preview([_workout(source_ref="webcal://secret-token.example/path")]),
        existing_events=[],
        health_payload=_ok_health(),
        live=False,
        **_paths(tmp_path),
    )
    state_text = (tmp_path / "training_calendar_scheduler.json").read_text()
    audit_text = (tmp_path / "assistant_audit.jsonl").read_text()
    combined = json.dumps(payload) + state_text + audit_text

    assert "webcal://secret-token.example/path" not in combined
    assert "secret-token" not in combined
    assert "redacted" in combined


def test_scheduler_reconcile_source_ref_drift_records_alias(tmp_path):
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    state.record_created(
        idempotency_key="rocky:training:2026-05-27:old",
        calendar_name="Calendar",
        title="Rocky: Training - Run: Recovery Run 60 min",
        start="2026-05-27T08:00:00+02:00",
        end="2026-05-27T10:30:00+02:00",
        event_uid="uid-old",
        create_audit_id="audit-old",
        metadata={"source_refs": ["trainingpeaks:old"], "kind": "training"},
    )
    existing = [
        {
            "summary": "Rocky: Training - Run: Recovery Run 60 min",
            "description": "Booked by: Rocky\nIdempotency key: rocky:training:2026-05-27:old",
            "start_local": "2026-05-27 08:00:00",
            "end_local": "2026-05-27 10:30:00",
            "all_day": False,
            "calendar": "Calendar",
        }
    ]

    payload = run_training_calendar_scheduler(
        planning_date="2026-05-23",
        preview_payload=_preview([_workout(source_ref="trainingpeaks:new")]),
        existing_events=existing,
        health_payload=_ok_health(),
        live=True,
        reconcile=True,
        fix_safe=True,
        **_paths(tmp_path),
    )

    assert payload["status"] == "skipped_duplicate"
    assert payload["reconcile_result"]["status"] == "ok"
    assert payload["reconcile_result"]["result_statuses"] == ["source_ref_drift_verified"]
    assert state.list_aliases(canonical_idempotency_key="rocky:training:2026-05-27:old")


def test_scheduler_reconcile_manual_review_can_dry_run_notify(tmp_path):
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    state.record_created(
        idempotency_key="rocky:training:2026-05-27:old",
        calendar_name="Calendar",
        title="Rocky: Training - Run: Recovery Run 60 min",
        start="2026-05-27T08:00:00+02:00",
        end="2026-05-27T10:30:00+02:00",
        event_uid="uid-old",
        create_audit_id="audit-old",
        metadata={"source_refs": ["trainingpeaks:old"], "kind": "training"},
    )
    existing = [
        {
            "summary": "Rocky: Training - Run: Recovery Run 60 min",
            "description": "Booked by: Rocky\nIdempotency key: rocky:training:2026-05-27:old",
            "start_local": "2026-05-27 08:00:00",
            "end_local": "2026-05-27 10:30:00",
            "all_day": False,
            "calendar": "Calendar",
        }
    ]

    payload = run_training_calendar_scheduler(
        planning_date="2026-05-23",
        preview_payload=_preview([]),
        existing_events=existing,
        health_payload=_ok_health(),
        live=True,
        reconcile=True,
        fix_safe=True,
        notify_failures=True,
        notification_dry_run=True,
        **_paths(tmp_path),
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "training_calendar_reconcile_attention_needed"
    assert payload["notification"]["status"] == "dry_run"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "attention_needed"
