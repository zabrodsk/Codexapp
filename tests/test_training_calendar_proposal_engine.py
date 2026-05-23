import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from training_calendar_proposal_engine import (
    add_working_days,
    build_training_calendar_proposals,
    infer_training_window,
    parse_title_duration_minutes,
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


def test_adds_three_working_days_from_saturday_to_wednesday():
    assert add_working_days("2026-05-23", 3).isoformat() == "2026-05-27"


def test_selects_workout_exactly_three_working_days_ahead(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        preview_payload=_preview(
            [
                _workout(date="2026-05-26", title="Run: Wrong day 60 min"),
                _workout(date="2026-05-27", title="Run: Target day 60 min"),
            ]
        ),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["target_date"] == "2026-05-27"
    assert payload["selected_workout_count"] == 1
    assert payload["proposals"][0]["workout"]["title"] == "Run: Target day 60 min"


def test_date_only_workout_defaults_to_full_morning_window_and_duration():
    inference = infer_training_window(_workout(title="Bike: Recovery Miles 1.00hrs", sport="bike"))

    assert inference["window_start"] == "08:00"
    assert inference["window_end"] == "10:30"
    assert inference["proposal_duration_minutes"] == 150
    assert inference["block_scope"] == "full_morning"
    assert inference["inferred_workout_duration_minutes"] == 60
    assert inference["duration_source"] == "title"
    assert inference["confidence"] == "medium"
    assert "date_only_workout_time_inferred" in inference["warnings"]


def test_title_duration_parsing_examples():
    assert parse_title_duration_minutes("Run: Recovery Run 60 min") == 60
    assert parse_title_duration_minutes("Bike: Recovery Miles 1.00hrs") == 60
    assert parse_title_duration_minutes("Run: Endurance Run Trail 4 h") == 240
    assert parse_title_duration_minutes("Run: Endurance Run Trail 3:30") == 210
    assert parse_title_duration_minutes("Run: Steady State Run 3x14min") == 42


def test_uses_sport_fallback_when_title_has_no_duration():
    inference = infer_training_window(_workout(title="Run: Easy aerobic", sport="run"))

    assert inference["inferred_workout_duration_minutes"] == 75
    assert inference["duration_source"] == "sport_fallback"
    assert inference["confidence"] == "low"


def test_timed_trainingpeaks_workout_uses_exact_window_and_high_confidence(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-22",
        preview_payload=_preview(
            [
                _workout(
                    date="2026-05-27",
                    planned_start_local="2026-05-27T08:15:00+02:00",
                    planned_end_local="2026-05-27T09:30:00+02:00",
                    title="Run: Timed workout",
                    confidence="high",
                    warnings=[],
                )
            ]
        ),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    inference = payload["proposals"][0]["inference"]
    proposal = payload["proposals"][0]["proposal"]
    assert inference["window_start"] == "08:15"
    assert inference["window_end"] == "09:30"
    assert inference["proposal_duration_minutes"] == 75
    assert inference["confidence"] == "high"
    assert proposal["start"] == "2026-05-27T08:15:00+02:00"
    assert proposal["end"] == "2026-05-27T09:30:00+02:00"


def test_monday_through_thursday_workout_produces_dry_run_proposal(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        preview_payload=_preview([_workout(date="2026-05-27")]),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    proposal = payload["proposals"][0]
    assert payload["status"] == "proposal"
    assert proposal["status"] == "proposal"
    assert proposal["audit_id"]
    assert proposal["idempotency_key"]
    assert proposal["proposal"]["calendar_write_attempted"] is False


def test_friday_workout_is_reported_as_policy_blocked(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-26",
        preview_payload=_preview([_workout(date="2026-05-29")]),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    proposal = payload["proposals"][0]
    assert payload["target_date"] == "2026-05-29"
    assert payload["status"] == "blocked"
    assert payload["reason"] == "proactive_booking_blocked_on_friday_saturday_sunday"
    assert proposal["status"] == "blocked"
    assert proposal["reason"] == "policy_blocked"
    assert "proactive_booking_blocked_on_friday_saturday_sunday" in proposal["proposal"]["policy_decision"]["reasons"]


def test_weekend_workout_can_be_reported_as_policy_blocked_with_target_override(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        target_date="2026-05-30",
        preview_payload=_preview([_workout(date="2026-05-30")]),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["proposals"][0]["reason"] == "policy_blocked"


def test_existing_calendar_conflict_blocks_proposal(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        preview_payload=_preview([_workout(date="2026-05-27")]),
        existing_events=[
            {
                "summary": "Existing meeting",
                "description": "",
                "start_local": "2026-05-27 08:30:00",
                "end_local": "2026-05-27 09:00:00",
                "all_day": False,
                "calendar": "Calendar",
            }
        ],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["proposals"][0]["reason"] == "no_available_slot"


def test_existing_rocky_duplicate_blocks_proposal(tmp_path):
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        preview_payload=_preview([_workout(date="2026-05-27", title="Run: Recovery Run 60 min")]),
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
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["proposals"][0]["reason"] == "duplicate_rocky_block"


def test_output_and_audit_do_not_include_secret_like_content(tmp_path):
    ledger_path = tmp_path / "assistant_audit.jsonl"
    payload = build_training_calendar_proposals(
        planning_date="2026-05-23",
        preview_payload=_preview(
            [
                _workout(
                    date="2026-05-27",
                    title="Auth token workout",
                    source_ref="trainingpeaks:safe-ref",
                )
            ]
        ),
        existing_events=[],
        ledger_path=ledger_path,
    )
    audit_payload = [event.__dict__ for event in AssistantAuditLog(ledger_path).read_all()]
    rendered = json.dumps({"payload": payload, "audit": audit_payload})

    assert "Auth token workout" not in rendered
    assert "webcal://" not in rendered
    assert "cookie" not in rendered.lower()
    assert "password" not in rendered.lower()
    assert payload["proposals"][0]["workout"]["title"] == "Planned workout"
