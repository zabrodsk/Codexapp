import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_prep_scheduler import build_meeting_prep_candidates, run_meeting_prep_scheduler


def _meeting():
    return {
        "meeting_key": "meeting:1",
        "title": "Jana portfolio update",
        "date": "2026-05-25",
        "start_local": "2026-05-25 10:30:00",
        "end_local": "2026-05-25 11:00:00",
        "participant_count": 1,
        "participant_domains": ["example.com"],
        "query_terms": ["jana", "portfolio"],
        "participants": [{"name": "Jana", "domain": "example.com"}],
        "calendar_event_ref": "apple-calendar:event:1",
    }


def test_weekend_scheduler_skips_without_writes(tmp_path):
    payload = run_meeting_prep_scheduler(
        planning_date="2026-05-24",
        live=True,
        notify=True,
        notification_dry_run=True,
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        ledger_path=tmp_path / "audit.jsonl",
        state_file=tmp_path / "state.json",
        now_local="2026-05-24T08:00:00+02:00",
    )

    assert payload["status"] == "skipped_weekend"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False


def test_candidates_include_context_status(monkeypatch):
    monkeypatch.setattr(
        "meeting_prep_scheduler.enrich_meeting_context",
        lambda meeting, **kwargs: {"status": "ok", "context_count": 2, "confidence": 0.8, "source_refs": ["people/jana.md"]},
    )
    payload = build_meeting_prep_candidates(
        planning_date="2026-05-25",
        meetings_payload={"status": "ok", "meetings": [_meeting()]},
        include_context=True,
    )

    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["context_status"] == "ok"


def test_due_meeting_brief_is_notified_and_no_calendar_write(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "meeting_prep_scheduler.enrich_meeting_context",
        lambda meeting, **kwargs: {"status": "ok", "context_count": 1, "confidence": 0.8, "source_refs": ["people/jana.md"], "memory": {"items": [{"title": "Jana", "summary": "Context"}]}, "notion_tasks": {"items": []}, "email_context": {"items": []}, "discord_context_notes": []},
    )
    monkeypatch.setattr(
        "meeting_prep_scheduler.ensure_meeting_prep_database_schema",
        lambda **kwargs: {"status": "ok", "notion_write_attempted": False},
    )
    monkeypatch.setattr(
        "meeting_prep_scheduler.upsert_meeting_prep_note",
        lambda *args, **kwargs: {"status": "created", "notion_write_attempted": True, "page_id": "page1"},
    )

    payload = run_meeting_prep_scheduler(
        planning_date="2026-05-25",
        live=True,
        notify=True,
        notification_dry_run=True,
        scheduler_db_path=tmp_path / "scheduler.sqlite3",
        ledger_path=tmp_path / "audit.jsonl",
        state_file=tmp_path / "state.json",
        now_local="2026-05-25T09:45:00+02:00",
        meetings_payload={"status": "ok", "meetings": [_meeting()]},
        capture_context_notes=False,
    )

    assert payload["status"] == "ok"
    assert payload["processed"][0]["notification"]["status"] == "dry_run"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is True
