import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_policy import stable_idempotency_key
from assistant_calendar_state import AssistantCalendarState
from assistant_calendar_writer import (
    _run_osascript_with_calendar_retry,
    create_calendar_block,
    delete_calendar_block,
)


def _paths(tmp_path):
    return {
        "state_db_path": tmp_path / "assistant_calendar.sqlite3",
        "ledger_path": tmp_path / "assistant_audit.jsonl",
        "scheduler_db_path": tmp_path / "assistant_scheduler.sqlite3",
    }


def _completed(stdout="uid-test\n", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["osascript"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _matching_event(title, start, end, idempotency_key, calendar="Calendar"):
    return {
        "summary": title,
        "start_local": start,
        "end_local": end,
        "all_day": False,
        "calendar": calendar,
        "location": "",
        "description": f"Booked by: Rocky\nIdempotency key: {idempotency_key}",
    }


def _task_key():
    return stable_idempotency_key(
        kind="task_focus",
        day="2026-05-25",
        start="07:00",
        duration_minutes=15,
        source_refs=["test:sprint3"],
    )


def test_create_requires_live_before_subprocess(tmp_path):
    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            source_refs=["test:sprint3"],
            live=False,
            **_paths(tmp_path),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False
    run_script.assert_not_called()


def test_weekend_create_blocks_before_applescript(tmp_path):
    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-24",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            live=True,
            existing_events=[],
            **_paths(tmp_path),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "policy_blocked"
    assert payload["calendar_write_attempted"] is False
    run_script.assert_not_called()


def test_conflict_blocks_before_applescript(tmp_path):
    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            live=True,
            existing_events=[
                {
                    "summary": "Existing",
                    "start_local": "2026-05-25 07:00:00",
                    "end_local": "2026-05-25 07:30:00",
                    "all_day": False,
                    "calendar": "Calendar",
                    "location": "",
                    "description": "",
                }
            ],
            **_paths(tmp_path),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "no_available_slot"
    assert payload["calendar_write_attempted"] is False
    run_script.assert_not_called()


def test_existing_active_state_and_matching_calendar_event_skips_duplicate(tmp_path):
    paths = _paths(tmp_path)
    key = _task_key()
    title = "Rocky: Task focus - Sprint 3 smoke"
    state = AssistantCalendarState(paths["state_db_path"])
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title=title,
        start="2026-05-25T07:00:00+02:00",
        end="2026-05-25T07:15:00+02:00",
        event_uid="uid-existing",
        create_audit_id="audit-existing",
    )

    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            label="Sprint 3 smoke",
            source_refs=["test:sprint3"],
            live=True,
            existing_events=[
                _matching_event(title, "2026-05-25 07:00:00", "2026-05-25 07:15:00", key)
            ],
            **paths,
        )

    assert payload["status"] == "skipped_duplicate"
    assert payload["calendar_write_attempted"] is False
    run_script.assert_not_called()


def test_create_records_audit_and_state(tmp_path):
    paths = _paths(tmp_path)
    key = _task_key()
    title = "Rocky: Task focus - Sprint 3 smoke"
    with (
        patch("assistant_calendar_writer._run_osascript_with_calendar_retry", return_value=_completed("uid-created\n")) as run_script,
        patch(
            "assistant_calendar_writer.query_events",
            return_value=[
                _matching_event(title, "2026-05-25 07:00:00", "2026-05-25 07:15:00", key)
            ],
        ),
    ):
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            label="Sprint 3 smoke",
            source_refs=["test:sprint3"],
            live=True,
            existing_events=[],
            **paths,
        )

    assert payload["status"] == "created"
    assert payload["calendar_write_attempted"] is True
    assert payload["calendar_event_created"] is True
    assert payload["event_uid"] == "uid-created"
    run_script.assert_called_once()
    row = AssistantCalendarState(paths["state_db_path"]).get(key)
    assert row["status"] == "active"
    assert row["event_uid"] == "uid-created"
    rows = [json.loads(line) for line in Path(paths["ledger_path"]).read_text().splitlines()]
    assert "calendar.write_requested" in {row["event_type"] for row in rows}
    assert "calendar.event_created" in {row["event_type"] for row in rows}


def test_create_applescript_failure_records_write_failed(tmp_path):
    paths = _paths(tmp_path)
    with patch(
        "assistant_calendar_writer._run_osascript_with_calendar_retry",
        return_value=_completed(stdout="", returncode=1, stderr="TCC denied"),
    ):
        payload = create_calendar_block(
            kind="task_focus",
            day="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            label="Sprint 3 smoke",
            source_refs=["test:sprint3"],
            live=True,
            existing_events=[],
            **paths,
        )

    assert payload["status"] == "failed"
    assert payload["reason"] == "osascript_create_failed"
    rows = [json.loads(line) for line in Path(paths["ledger_path"]).read_text().splitlines()]
    assert "calendar.write_failed" in {row["event_type"] for row in rows}


def test_delete_requires_live_before_subprocess(tmp_path):
    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = delete_calendar_block(
            idempotency_key="rocky:test",
            live=False,
            **_paths(tmp_path),
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False
    run_script.assert_not_called()


def test_delete_refuses_non_rocky_state(tmp_path):
    paths = _paths(tmp_path)
    state = AssistantCalendarState(paths["state_db_path"])
    state.record_created(
        idempotency_key="rocky:test",
        calendar_name="Calendar",
        title="Not Rocky",
        start="2026-05-25T07:00:00+02:00",
        end="2026-05-25T07:15:00+02:00",
        event_uid="uid-test",
        create_audit_id="audit-test",
    )

    with patch("assistant_calendar_writer._run_osascript_with_calendar_retry") as run_script:
        payload = delete_calendar_block(
            idempotency_key="rocky:test",
            calendar_name="Calendar",
            live=True,
            **paths,
        )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "rocky_ownership_check_failed"
    run_script.assert_not_called()


def test_delete_records_audit_and_state(tmp_path):
    paths = _paths(tmp_path)
    key = _task_key()
    title = "Rocky: Task focus - Sprint 3 smoke"
    state = AssistantCalendarState(paths["state_db_path"])
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title=title,
        start="2026-05-25T07:00:00+02:00",
        end="2026-05-25T07:15:00+02:00",
        event_uid="uid-created",
        create_audit_id="audit-created",
    )

    with (
        patch("assistant_calendar_writer._run_eventkit_delete", return_value=_completed("1\n")),
        patch(
            "assistant_calendar_writer.query_events",
            side_effect=[
                [_matching_event(title, "2026-05-25 07:00:00", "2026-05-25 07:15:00", key)],
                [],
            ],
        ),
    ):
        payload = delete_calendar_block(
            idempotency_key=key,
            calendar_name="Calendar",
            live=True,
            **paths,
        )

    assert payload["status"] == "deleted"
    assert payload["calendar_event_deleted"] is True
    assert payload["delete_method"] == "eventkit"
    row = AssistantCalendarState(paths["state_db_path"]).get(key)
    assert row["status"] == "deleted"
    assert row["delete_audit_id"] == payload["audit_id"]
    rows = [json.loads(line) for line in Path(paths["ledger_path"]).read_text().splitlines()]
    assert "calendar.event_deleted" in {row["event_type"] for row in rows}
    deleted = [row for row in rows if row["event_type"] == "calendar.event_deleted"][0]
    assert deleted["artifacts"]["delete_method"] == "eventkit"


def test_delete_uses_applescript_fallback_when_eventkit_fails(tmp_path):
    paths = _paths(tmp_path)
    key = _task_key()
    title = "Rocky: Task focus - Sprint 3 smoke"
    state = AssistantCalendarState(paths["state_db_path"])
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title=title,
        start="2026-05-25T07:00:00+02:00",
        end="2026-05-25T07:15:00+02:00",
        event_uid="uid-created",
        create_audit_id="audit-created",
    )

    with (
        patch("assistant_calendar_writer._run_eventkit_delete", return_value=_completed(stdout="", returncode=1, stderr="eventkit failed")),
        patch("assistant_calendar_writer._run_osascript_with_calendar_retry", return_value=_completed("1\n")) as fallback,
        patch(
            "assistant_calendar_writer.query_events",
            side_effect=[
                [_matching_event(title, "2026-05-25 07:00:00", "2026-05-25 07:15:00", key)],
                [],
            ],
        ),
    ):
        payload = delete_calendar_block(
            idempotency_key=key,
            calendar_name="Calendar",
            live=True,
            **paths,
        )

    assert payload["status"] == "deleted"
    assert payload["calendar_event_deleted"] is True
    assert payload["delete_method"] == "osascript_fallback"
    fallback.assert_called_once()


def test_delete_failure_distinguishes_eventkit_and_applescript_failures(tmp_path):
    paths = _paths(tmp_path)
    key = _task_key()
    title = "Rocky: Task focus - Sprint 3 smoke"
    state = AssistantCalendarState(paths["state_db_path"])
    state.record_created(
        idempotency_key=key,
        calendar_name="Calendar",
        title=title,
        start="2026-05-25T07:00:00+02:00",
        end="2026-05-25T07:15:00+02:00",
        event_uid="uid-created",
        create_audit_id="audit-created",
    )

    with (
        patch("assistant_calendar_writer._run_eventkit_delete", return_value=_completed(stdout="", returncode=1, stderr="eventkit failed")),
        patch("assistant_calendar_writer._run_osascript_with_calendar_retry", return_value=_completed(stdout="", returncode=124, stderr="osascript timeout")),
        patch(
            "assistant_calendar_writer.query_events",
            return_value=[
                _matching_event(title, "2026-05-25 07:00:00", "2026-05-25 07:15:00", key)
            ],
        ),
    ):
        payload = delete_calendar_block(
            idempotency_key=key,
            calendar_name="Calendar",
            live=True,
            **paths,
        )

    assert payload["status"] == "failed"
    assert payload["reason"] == "eventkit_delete_failed_and_osascript_fallback_failed"
    assert payload["delete_method"] == "eventkit_then_osascript_fallback"
    rows = [json.loads(line) for line in Path(paths["ledger_path"]).read_text().splitlines()]
    failed = [row for row in rows if row["event_type"] == "calendar.delete_failed"][0]
    assert failed["reason"] == "eventkit_delete_failed_and_osascript_fallback_failed"


def test_calendar_not_running_launches_and_retries():
    first = _completed(stdout="", returncode=1, stderr="Application isn't running. (-600)")
    launch = _completed(stdout="", returncode=0)
    second = _completed(stdout="uid-ok\n", returncode=0)
    with patch("assistant_calendar_writer.subprocess.run", side_effect=[first, launch, second]) as run:
        result = _run_osascript_with_calendar_retry("return \"ok\"", [])

    assert result.returncode == 0
    assert result.stdout == "uid-ok\n"
    assert run.call_count == 3
    assert run.call_args_list[1].args[0] == ["open", "-a", "Calendar"]


def test_osascript_timeout_returns_failed_process():
    with patch(
        "assistant_calendar_writer.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["osascript"], timeout=30),
    ):
        result = _run_osascript_with_calendar_retry("return \"ok\"", [])

    assert result.returncode == 124
    assert "timed out" in result.stderr
