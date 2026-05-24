import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_outcome_scheduler import run_meeting_outcome_scheduler


def _candidates():
    return {
        "status": "ok",
        "candidate_count": 1,
        "candidates": [
            {
                "meeting_key": "meeting:jana",
                "source_ref": "obsidian-meeting-outcome:abc",
                "title": "Jana investor update",
                "meeting_date": "2026-05-25",
                "evidence_hash": "evidence1",
                "structured_lines": [{"section": "Follow-ups", "text": "Dusan: send Jana the updated deck."}],
            }
        ],
    }


def test_weekend_scheduler_skips_without_writes(tmp_path):
    payload = run_meeting_outcome_scheduler(
        planning_date="2026-05-24",
        live=True,
        notify_failures=True,
        notification_dry_run=True,
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        audit_ledger_path=tmp_path / "audit.jsonl",
        ledger_db_path=tmp_path / "outcomes.sqlite3",
        state_file=tmp_path / "state.json",
        now_local="2026-05-24T09:00:00+02:00",
    )

    assert payload["status"] == "skipped_weekend"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False


def test_scheduler_live_applies_outcome_without_calendar_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr("meeting_outcome_task_applier.ensure_task_database_schema", lambda **kwargs: {"status": "ok"})
    monkeypatch.setattr("meeting_outcome_task_applier.list_open_tasks", lambda **kwargs: {"status": "ok", "tasks": []})
    monkeypatch.setattr("meeting_outcome_task_applier.upsert_task", lambda task, **kwargs: {"status": "created", "page_id": "page1", "dedupe_key": task["dedupe_key"]})
    monkeypatch.setattr("investor_relationship_memory.promote_cross_agent_memory", lambda **kwargs: {"status": "ok", "path": "/vault/meeting.md", "note_type": "meeting", "action": "created"})
    monkeypatch.setattr("meeting_outcome_scheduler.update_meeting_prep_outcome", lambda *args, **kwargs: {"status": "updated", "notion_write_attempted": True})

    payload = run_meeting_outcome_scheduler(
        planning_date="2026-05-25",
        live=True,
        candidates_payload=_candidates(),
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        audit_ledger_path=tmp_path / "audit.jsonl",
        ledger_db_path=tmp_path / "outcomes.sqlite3",
        state_file=tmp_path / "state.json",
        now_local="2026-05-25T10:00:00+02:00",
        use_llm=False,
    )

    assert payload["status"] == "ok"
    assert payload["tasks_created"] == 1
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is True


def test_scheduler_skips_already_applied_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr("meeting_outcome_task_applier.ensure_task_database_schema", lambda **kwargs: {"status": "ok"})
    monkeypatch.setattr("meeting_outcome_task_applier.list_open_tasks", lambda **kwargs: {"status": "ok", "tasks": []})
    monkeypatch.setattr("meeting_outcome_task_applier.upsert_task", lambda task, **kwargs: {"status": "created", "page_id": "page1", "dedupe_key": task["dedupe_key"]})
    monkeypatch.setattr("investor_relationship_memory.promote_cross_agent_memory", lambda **kwargs: {"status": "ok", "path": "/vault/meeting.md", "note_type": "meeting", "action": "created"})
    monkeypatch.setattr("meeting_outcome_scheduler.update_meeting_prep_outcome", lambda *args, **kwargs: {"status": "updated", "notion_write_attempted": True})
    scheduler_db = tmp_path / "scheduler.sqlite3"
    ledger_db = tmp_path / "outcomes.sqlite3"

    first = run_meeting_outcome_scheduler(
        planning_date="2026-05-25",
        live=True,
        candidates_payload=_candidates(),
        scheduler_db_path=scheduler_db,
        audit_ledger_path=tmp_path / "audit.jsonl",
        ledger_db_path=ledger_db,
        state_file=tmp_path / "state1.json",
        now_local="2026-05-25T10:00:00+02:00",
        use_llm=False,
    )
    second = run_meeting_outcome_scheduler(
        planning_date="2026-05-25",
        live=True,
        candidates_payload=_candidates(),
        scheduler_db_path=scheduler_db,
        audit_ledger_path=tmp_path / "audit.jsonl",
        ledger_db_path=ledger_db,
        state_file=tmp_path / "state2.json",
        now_local="2026-05-25T10:30:00+02:00",
        use_llm=False,
    )

    assert first["status"] == "ok"
    assert second["status"] == "skipped_duplicate_outcomes"
    assert second["notion_write_attempted"] is False
