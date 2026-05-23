import json
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState
from training_calendar_live_booking import book_training_calendar_proposal
from training_calendar_proposal_engine import build_training_calendar_proposals


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
    return {"status": "ok", "blocked_checks": []}


def _proposal_key(*, planning_date="2026-05-23", preview_payload=None, existing_events=None):
    payload = build_training_calendar_proposals(
        planning_date=planning_date,
        preview_payload=preview_payload or _preview([_workout()]),
        existing_events=existing_events or [],
        write_audit=False,
    )
    return payload["proposals"][0]["idempotency_key"]


def test_training_calendar_book_refuses_without_live_before_writer():
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key="rocky:training:test",
            live=False,
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False
    writer.assert_not_called()


def test_unknown_idempotency_key_blocks_before_writer():
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key="rocky:training:missing",
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "training_calendar_proposal_not_found"
    assert payload["calendar_write_attempted"] is False
    writer.assert_not_called()


def test_policy_blocked_weekend_proposal_blocks_before_writer():
    preview = _preview([_workout(date="2026-05-29")])
    key = _proposal_key(planning_date="2026-05-26", preview_payload=preview)
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-26",
            preview_payload=preview,
            existing_events=[],
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "policy_blocked"
    assert payload["calendar_write_attempted"] is False
    writer.assert_not_called()


def test_conflict_blocks_before_writer():
    conflict = [
        {
            "summary": "Existing meeting",
            "description": "",
            "start_local": "2026-05-27 08:30:00",
            "end_local": "2026-05-27 09:00:00",
            "all_day": False,
            "calendar": "Calendar",
        }
    ]
    key = _proposal_key(existing_events=conflict)
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=conflict,
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "no_available_slot"
    assert payload["calendar_write_attempted"] is False
    writer.assert_not_called()


def test_duplicate_proposal_blocks_before_writer():
    duplicate = [
        {
            "summary": "Rocky: Training - Run: Recovery Run 60 min",
            "description": "Booked by: Rocky",
            "start_local": "2026-05-27 08:00:00",
            "end_local": "2026-05-27 10:30:00",
            "all_day": False,
            "calendar": "Calendar",
        }
    ]
    key = _proposal_key(existing_events=duplicate)
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=duplicate,
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "duplicate_rocky_block"
    assert payload["calendar_write_attempted"] is False
    writer.assert_not_called()


def test_successful_live_booking_calls_writer_with_exact_proposal_fields():
    key = _proposal_key()
    with patch(
        "training_calendar_live_booking.create_calendar_block",
        return_value={
            "status": "created",
            "reason": "TrainingPeaks planned workout 3 working days ahead",
            "audit_id": "audit-created",
            "idempotency_key": key,
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as writer:
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "created"
    assert payload["mode"] == "live"
    assert payload["calendar_write_attempted"] is True
    writer.assert_called_once()
    kwargs = writer.call_args.kwargs
    assert kwargs["kind"] == "training"
    assert kwargs["day"] == "2026-05-27"
    assert kwargs["window_start"] == "08:00"
    assert kwargs["window_end"] == "10:30"
    assert kwargs["duration_minutes"] == 150
    assert kwargs["label"] == "Run: Recovery Run 60 min"
    assert kwargs["source_refs"] == ["trainingpeaks:test"]
    assert kwargs["live"] is True


def test_existing_writer_duplicate_is_successful_without_second_write(tmp_path):
    key = _proposal_key()
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title="Rocky: Training - Run: Recovery Run 60 min",
        start="2026-05-27T08:00:00+02:00",
        end="2026-05-27T10:30:00+02:00",
        event_uid="uid-existing",
        create_audit_id="audit-existing",
    )
    with patch(
        "training_calendar_live_booking.create_calendar_block",
        return_value={
            "status": "skipped_duplicate",
            "reason": "duplicate_existing_active_event",
            "audit_id": "audit-duplicate",
            "idempotency_key": key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        },
    ):
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[
                {
                    "summary": "Rocky: Training - Run: Recovery Run 60 min",
                    "description": "Booked by: Rocky",
                    "start_local": "2026-05-27 08:00:00",
                    "end_local": "2026-05-27 10:30:00",
                    "all_day": False,
                    "calendar": "Calendar",
                }
            ],
            live=True,
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
            health_payload=_ok_health(),
        )

    assert payload["status"] == "skipped_duplicate"
    assert payload["mode"] == "preflight"
    assert payload["calendar_write_attempted"] is False


def test_writer_failure_is_propagated_safely():
    key = _proposal_key()
    with patch(
        "training_calendar_live_booking.create_calendar_block",
        return_value={
            "status": "failed",
            "reason": "osascript_create_failed",
            "audit_id": "audit-failed",
            "idempotency_key": key,
            "calendar_write_attempted": True,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        },
    ):
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            live=True,
            health_payload=_ok_health(),
        )

    assert payload["status"] == "failed"
    assert payload["reason"] == "osascript_create_failed"
    assert payload["calendar_write_attempted"] is True
    assert payload["calendar_event_created"] is False


def test_calendar_health_failure_blocks_before_writer():
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key="rocky:training:test",
            planning_date="2026-05-23",
            preview_payload=_preview([_workout()]),
            existing_events=[],
            live=True,
            health_payload={"status": "blocked", "blocked_checks": ["eventkit"]},
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "calendar_write_health_not_ok"
    assert payload["health"] == {"status": "blocked", "blocked_checks": ["eventkit"]}
    writer.assert_not_called()


def test_output_redacts_secret_like_source_refs_before_write():
    preview = _preview([_workout(source_ref="webcal://token-secret")])
    key = _proposal_key(preview_payload=preview)
    with patch("training_calendar_live_booking.create_calendar_block") as writer:
        payload = book_training_calendar_proposal(
            idempotency_key=key,
            planning_date="2026-05-23",
            preview_payload=preview,
            existing_events=[],
            live=True,
            health_payload=_ok_health(),
        )

    rendered = json.dumps(payload)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "unsafe_training_source_ref"
    assert "webcal://" not in rendered
    assert "token-secret" not in rendered
    writer.assert_not_called()
