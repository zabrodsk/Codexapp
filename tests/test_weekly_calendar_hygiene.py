import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState
from weekly_calendar_hygiene import inspect_weekly_calendar_hygiene


def _event(summary, start, end, description, *, calendar="Calendar"):
    return {"summary": summary, "start_local": start, "end_local": end, "all_day": False, "calendar": calendar, "description": description, "location": ""}


def test_calendar_hygiene_detects_duplicates_stale_orphans_metadata_and_weekends(tmp_path):
    state_db = tmp_path / "calendar.sqlite3"
    state = AssistantCalendarState(state_db)
    state.record_created(
        idempotency_key="rocky:task_focus:2026-05-26:missing",
        calendar_name="Calendar",
        title="Rocky: Task focus - Missing",
        start="2026-05-26T13:00:00+02:00",
        end="2026-05-26T14:00:00+02:00",
        event_uid="uid-missing",
        create_audit_id="audit-1",
        metadata={},
    )
    events = [
        _event("Rocky: Coding focus - A", "2026-05-25 13:00:00", "2026-05-25 14:00:00", "Booked by: Rocky\nIdempotency key: rocky:coding_focus:2026-05-25:a token=secret"),
        _event("Rocky: Coding focus - A", "2026-05-25 13:30:00", "2026-05-25 14:30:00", "Booked by: Rocky\nIdempotency key: rocky:coding_focus:2026-05-25:a"),
        _event("Rocky: Task focus - Orphan", "2026-05-26 15:00:00", "2026-05-26 16:00:00", "Booked by: Rocky\nIdempotency key: rocky:task_focus:2026-05-26:orphan"),
        _event("Rocky: Email triage - unread attention", "2026-05-29 13:00:00", "2026-05-29 13:30:00", "Booked by: Rocky\nIdempotency key: rocky:email_triage:2026-05-29:x"),
        _event("Rocky: Task focus - Missing metadata", "2026-05-27 15:00:00", "2026-05-27 16:00:00", "plain notes"),
    ]

    payload = inspect_weekly_calendar_hygiene(start_date="2026-05-25", days=7, events=events, state_db_path=state_db)

    assert payload["status"] == "manual_review_required"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    assert payload["duplicate_rocky_blocks"]
    assert payload["stale_state_candidates"][0]["idempotency_key"] == "rocky:task_focus:2026-05-26:missing"
    assert payload["orphan_rocky_events"]
    assert payload["missing_metadata_events"]
    assert payload["weekend_policy_violations"]
    assert "secret" not in json.dumps(payload).lower()


def test_mark_stale_requires_live_and_only_mutates_state(tmp_path):
    state_db = tmp_path / "calendar.sqlite3"
    state = AssistantCalendarState(state_db)
    state.record_created(
        idempotency_key="rocky:task_focus:2026-05-26:missing",
        calendar_name="Calendar",
        title="Rocky: Task focus - Missing",
        start="2026-05-26T13:00:00+02:00",
        end="2026-05-26T14:00:00+02:00",
        event_uid="uid-missing",
        create_audit_id="audit-1",
        metadata={},
    )

    blocked = inspect_weekly_calendar_hygiene(start_date="2026-05-25", events=[], state_db_path=state_db, mark_stale=True, live=False)
    assert blocked["status"] == "blocked"
    assert state.get("rocky:task_focus:2026-05-26:missing")["status"] == "active"

    live = inspect_weekly_calendar_hygiene(start_date="2026-05-25", events=[], state_db_path=state_db, ledger_path=tmp_path / "audit.jsonl", mark_stale=True, live=True)
    assert live["state_mutated"] is True
    assert live["calendar_write_attempted"] is False
    assert state.get("rocky:task_focus:2026-05-26:missing")["status"] == "stale"
