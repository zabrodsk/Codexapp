import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_outcome_task_applier import apply_meeting_outcome_tasks


def _outcome():
    return {
        "follow_up_tasks": [
            {
                "title": "send Jana the updated deck",
                "source": "Meeting",
                "source_ref": "meeting:1:followup:1",
                "owner": "Dusan",
                "requires_dusan_action": True,
                "priority": "High",
                "confidence": 0.82,
                "estimated_effort_minutes": 30,
            }
        ]
    }


def test_task_applier_dry_run_resolves_without_notion_write():
    payload = apply_meeting_outcome_tasks(_outcome(), live=False, existing_tasks=[])

    assert payload["status"] == "dry_run"
    assert payload["created_count"] == 1
    assert payload["notion_write_attempted"] is False


def test_task_applier_live_uses_identity_and_upsert(monkeypatch):
    monkeypatch.setattr("meeting_outcome_task_applier.ensure_task_database_schema", lambda **kwargs: {"status": "ok"})

    def fake_upsert(task, **kwargs):
        return {"status": "created", "page_id": "page1", "dedupe_key": task["dedupe_key"]}

    payload = apply_meeting_outcome_tasks(_outcome(), live=True, existing_tasks=[], upsert_func=fake_upsert)

    assert payload["status"] == "ok"
    assert payload["task_refs"][0]["page_id"] == "page1"
    assert payload["notion_write_attempted"] is True
