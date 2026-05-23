import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState
from assistant_calendar_status import (
    calendar_write_health,
    inspect_calendar_block,
    reconcile_calendar_blocks,
)


KEY = "rocky:task_focus:2026-05-25:status-test"
TITLE = "Rocky: Task focus - Status test"
START = "2026-05-25T07:00:00+02:00"
END = "2026-05-25T07:15:00+02:00"


def _event(description=None, calendar="Calendar"):
    return {
        "summary": TITLE,
        "start_local": "2026-05-25 07:00:00",
        "end_local": "2026-05-25 07:15:00",
        "all_day": False,
        "calendar": calendar,
        "location": "private room",
        "description": description
        if description is not None
        else f"Booked by: Rocky\nIdempotency key: {KEY}\nsecret raw notes",
    }


def _state(tmp_path):
    return AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")


def _record_active(tmp_path, *, calendar="Calendar"):
    state = _state(tmp_path)
    state.record_created(
        idempotency_key=KEY,
        calendar_name=calendar,
        title=TITLE,
        start=START,
        end=END,
        event_uid="uid-status",
        create_audit_id="audit-created",
        metadata={"source_refs": ["test:status"]},
    )
    return state


def test_status_reports_active_verified_without_raw_description(tmp_path):
    _record_active(tmp_path)
    with patch("assistant_calendar_status.query_events", return_value=[_event()]):
        payload = inspect_calendar_block(
            idempotency_key=KEY,
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
        )

    assert payload["status"] == "active_verified"
    assert payload["calendar_match_count"] == 1
    rendered = json.dumps(payload)
    assert "secret raw notes" not in rendered
    assert "description" not in rendered
    assert payload["calendar_matches"][0]["has_idempotency_key"] is True


def test_status_reports_stale_state_candidate_when_active_event_missing(tmp_path):
    _record_active(tmp_path)
    with patch("assistant_calendar_status.query_events", return_value=[]):
        payload = inspect_calendar_block(
            idempotency_key=KEY,
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
        )

    assert payload["status"] == "stale_state_candidate"
    assert payload["calendar_match_count"] == 0


def test_status_reports_deleted_verified_when_deleted_event_absent(tmp_path):
    state = _record_active(tmp_path)
    state.mark_deleted(idempotency_key=KEY, delete_audit_id="audit-deleted")

    with patch("assistant_calendar_status.query_events", return_value=[]):
        payload = inspect_calendar_block(
            idempotency_key=KEY,
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
        )

    assert payload["status"] == "deleted_verified"


def test_status_reports_orphan_calendar_event_when_deleted_event_remains(tmp_path):
    state = _record_active(tmp_path)
    state.mark_deleted(idempotency_key=KEY, delete_audit_id="audit-deleted")

    with patch("assistant_calendar_status.query_events", return_value=[_event()]):
        payload = inspect_calendar_block(
            idempotency_key=KEY,
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
        )

    assert payload["status"] == "orphan_calendar_event"
    assert payload["calendar_match_count"] == 1


def test_status_reports_state_missing(tmp_path):
    payload = inspect_calendar_block(
        idempotency_key=KEY,
        state_db_path=tmp_path / "assistant_calendar.sqlite3",
    )

    assert payload["status"] == "state_missing"
    assert payload["calendar_match_count"] == 0


def test_status_reports_calendar_mismatch(tmp_path):
    _record_active(tmp_path, calendar="Personal")
    with patch("assistant_calendar_status.query_events", return_value=[_event(calendar="Personal")]):
        payload = inspect_calendar_block(
            idempotency_key=KEY,
            calendar_name="Calendar",
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
        )

    assert payload["status"] == "calendar_mismatch"


def test_reconcile_without_mark_stale_does_not_mutate_state(tmp_path):
    state = _record_active(tmp_path)

    with patch("assistant_calendar_status.query_events", return_value=[]):
        payload = reconcile_calendar_blocks(
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
            ledger_path=tmp_path / "assistant_audit.jsonl",
            mark_stale=False,
        )

    assert payload["checked_count"] == 1
    assert payload["marked_stale_count"] == 0
    assert state.get(KEY)["status"] == "active"
    assert not (tmp_path / "assistant_audit.jsonl").exists()


def test_reconcile_with_mark_stale_marks_only_active_missing_state(tmp_path):
    state = _record_active(tmp_path)

    with patch("assistant_calendar_status.query_events", return_value=[]):
        payload = reconcile_calendar_blocks(
            state_db_path=tmp_path / "assistant_calendar.sqlite3",
            ledger_path=tmp_path / "assistant_audit.jsonl",
            mark_stale=True,
        )

    assert payload["checked_count"] == 1
    assert payload["marked_stale_count"] == 1
    assert state.get(KEY)["status"] == "stale"
    events = [json.loads(line) for line in (tmp_path / "assistant_audit.jsonl").read_text().splitlines()]
    assert "calendar.state_marked_stale" in {event["event_type"] for event in events}
    assert "calendar.state_reconciled" in {event["event_type"] for event in events}


def test_calendar_write_health_checks_readiness_without_creating_events(tmp_path):
    db_path = tmp_path / "Calendar.sqlitedb"
    db_path.touch()
    runs = [
        subprocess.CompletedProcess(args=["swift"], returncode=0, stdout="3\n", stderr=""),
        subprocess.CompletedProcess(args=["osascript"], returncode=0, stdout="Calendar\n", stderr=""),
    ]

    with (
        patch("assistant_calendar_status.query_events", return_value=[]),
        patch("assistant_calendar_status.shutil.which", return_value="/usr/bin/swift"),
        patch("assistant_calendar_status.subprocess.run", side_effect=runs),
    ):
        payload = calendar_write_health(
            db_path=db_path,
            ledger_path=tmp_path / "assistant_audit.jsonl",
        )

    assert payload["status"] == "ok"
    assert payload["calendar_write_attempted"] is False
    assert payload["checks"]["calendar_db"]["status"] == "ok"
    assert payload["checks"]["eventkit"]["authorization"] == "authorized"
    events = [json.loads(line) for line in (tmp_path / "assistant_audit.jsonl").read_text().splitlines()]
    assert events[0]["event_type"] == "calendar.write_health_checked"
