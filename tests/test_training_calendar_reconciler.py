import json
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_policy import stable_idempotency_key
from assistant_calendar_state import AssistantCalendarState
from training_calendar_reconciler import reconcile_training_calendar


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


def _state(tmp_path):
    return AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")


def _record_training_block(tmp_path, *, key="rocky:training:2026-05-27:old", source_ref="trainingpeaks:test", title="Run: Recovery Run 60 min", start="2026-05-27T08:00:00+02:00", end="2026-05-27T10:30:00+02:00"):
    state = _state(tmp_path)
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title=f"Rocky: Training - {title}",
        start=start,
        end=end,
        event_uid="uid-old",
        create_audit_id="audit-old",
        metadata={"source_refs": [source_ref], "kind": "training"},
    )
    return state


def _event(*, key="rocky:training:2026-05-27:old", title="Run: Recovery Run 60 min", start="2026-05-27 08:00:00", end="2026-05-27 10:30:00"):
    return {
        "summary": f"Rocky: Training - {title}",
        "description": f"Booked by: Rocky\nIdempotency key: {key}",
        "start_local": start,
        "end_local": end,
        "all_day": False,
        "calendar": "Calendar",
    }


def test_source_ref_drift_records_alias_without_calendar_write(tmp_path):
    state = _record_training_block(tmp_path, source_ref="trainingpeaks:old")
    current = _workout(source_ref="trainingpeaks:new")
    current_key = stable_idempotency_key(
        kind="training",
        day="2026-05-27",
        start="08:00",
        duration_minutes=150,
        source_refs=["trainingpeaks:new"],
    )

    payload = reconcile_training_calendar(
        planning_date="2026-05-23",
        preview_payload=_preview([current]),
        existing_events=[_event()],
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
        fix_safe=True,
        live=True,
    )

    assert payload["status"] == "ok"
    assert payload["calendar_write_attempted"] is False
    assert payload["results"][0]["status"] == "source_ref_drift_verified"
    assert state.resolve_alias(current_key)["idempotency_key"] == "rocky:training:2026-05-27:old"


def test_reconcile_without_fix_safe_does_not_record_alias(tmp_path):
    state = _record_training_block(tmp_path, source_ref="trainingpeaks:old")
    current = _workout(source_ref="trainingpeaks:new")
    current_key = stable_idempotency_key(
        kind="training",
        day="2026-05-27",
        start="08:00",
        duration_minutes=150,
        source_refs=["trainingpeaks:new"],
    )

    payload = reconcile_training_calendar(
        planning_date="2026-05-23",
        preview_payload=_preview([current]),
        existing_events=[_event()],
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
        fix_safe=False,
        live=False,
    )

    assert payload["results"][0]["status"] == "source_ref_drift_verified"
    assert state.resolve_alias(current_key) is None


def test_safe_non_overlapping_move_creates_new_before_deleting_old(tmp_path):
    _record_training_block(tmp_path)
    current = _workout(
        planned_start_local="2026-05-27T11:00:00+02:00",
        planned_end_local="2026-05-27T13:30:00+02:00",
    )
    calls = []

    def create(**kwargs):
        calls.append(("create", kwargs["window_start"], kwargs["window_end"]))
        return {
            "status": "created",
            "idempotency_key": "rocky:training:2026-05-27:new",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        }

    def delete(**kwargs):
        calls.append(("delete", kwargs["idempotency_key"]))
        return {
            "status": "deleted",
            "idempotency_key": kwargs["idempotency_key"],
            "calendar_write_attempted": True,
            "calendar_event_created": False,
            "calendar_event_deleted": True,
        }

    with (
        patch("training_calendar_reconciler.create_calendar_block", side_effect=create),
        patch("training_calendar_reconciler.delete_calendar_block", side_effect=delete),
    ):
        payload = reconcile_training_calendar(
            planning_date="2026-05-23",
            preview_payload=_preview([current]),
            existing_events=[_event()],
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
            fix_safe=True,
            live=True,
            health_payload={"status": "ok"},
        )

    assert payload["results"][0]["status"] == "moved_safe_fix_applied"
    assert calls == [("create", "11:00", "13:30"), ("delete", "rocky:training:2026-05-27:old")]


def test_create_failure_leaves_old_block_active(tmp_path):
    state = _record_training_block(tmp_path)
    current = _workout(
        planned_start_local="2026-05-27T11:00:00+02:00",
        planned_end_local="2026-05-27T13:30:00+02:00",
    )

    with (
        patch("training_calendar_reconciler.create_calendar_block", return_value={"status": "failed", "reason": "create_failed", "calendar_write_attempted": True}),
        patch("training_calendar_reconciler.delete_calendar_block") as delete,
    ):
        payload = reconcile_training_calendar(
            planning_date="2026-05-23",
            preview_payload=_preview([current]),
            existing_events=[_event()],
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
            fix_safe=True,
            live=True,
            health_payload={"status": "ok"},
        )

    assert payload["results"][0]["status"] == "manual_review_required"
    assert payload["results"][0]["reason"] == "safe_move_create_failed"
    assert state.get("rocky:training:2026-05-27:old")["status"] == "active"
    delete.assert_not_called()


def test_weekend_move_blocks_for_manual_review(tmp_path):
    _record_training_block(tmp_path)
    current = _workout(date="2026-05-29")
    payload = reconcile_training_calendar(
        planning_date="2026-05-23",
        preview_payload=_preview([current]),
        existing_events=[_event()],
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
        fix_safe=True,
        live=True,
    )

    assert payload["results"][0]["status"] == "weekend_policy_blocked"
    assert payload["calendar_write_attempted"] is False


def test_cancelled_candidate_blocks_for_manual_review(tmp_path):
    _record_training_block(tmp_path)
    payload = reconcile_training_calendar(
        planning_date="2026-05-23",
        preview_payload=_preview([]),
        existing_events=[_event()],
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
        fix_safe=True,
        live=True,
    )

    assert payload["results"][0]["status"] == "cancelled_candidate"
    assert payload["manual_review_required"] is True


def test_output_redacts_sensitive_training_fields(tmp_path):
    _record_training_block(tmp_path)
    payload = reconcile_training_calendar(
        planning_date="2026-05-23",
        preview_payload=_preview([
            _workout(source_ref="webcal://secret-token", title="Run: Recovery Run 60 min")
        ]),
        existing_events=[_event()],
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
        fix_safe=False,
        live=False,
    )

    rendered = json.dumps(payload)
    assert "secret-token" not in rendered
    assert "webcal://" not in rendered
