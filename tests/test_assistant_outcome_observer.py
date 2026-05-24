import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_store import AssistantLearningStore
from assistant_outcome_observer import collect_outcomes


def test_outcome_collection_is_dry_run_unless_live(tmp_path):
    db = tmp_path / "learning.sqlite3"
    payload = collect_outcomes(live=False, learning_db_path=db, calendar_blocks=[{"idempotency_key": "rocky:email:1", "title": "Rocky: Email triage - unread attention", "start": "2026-05-25T13:00:00+02:00", "end": "2026-05-25T13:30:00+02:00", "status": "active", "updated_at": "2026-05-25T12:00:00+00:00"}], job_runs=[], dead_letters=[])
    assert payload["outcome_count"] == 1
    assert payload["written_count"] == 0
    assert not db.exists()


def test_live_outcome_collection_writes_sanitized_rows(tmp_path):
    db = tmp_path / "learning.sqlite3"
    payload = collect_outcomes(live=True, learning_db_path=db, ledger_path=tmp_path / "audit.jsonl", calendar_blocks=[{"idempotency_key": "rocky:coding:1", "title": "Rocky: Coding focus - Matchbook", "start": "2026-05-25T12:30:00+02:00", "end": "2026-05-25T14:00:00+02:00", "status": "active", "updated_at": "2026-05-25T12:00:00+00:00", "metadata_json": "secret token"}], job_runs=[{"job_name": "daily_personal_briefing", "run_id": "run1", "status": "succeeded", "created_at": "2026-05-25T10:00:00+00:00", "summary": "posted"}], dead_letters=[])
    assert payload["written_count"] == 2
    rows = AssistantLearningStore(db).list_outcomes(limit=10)
    assert {row["lane"] for row in rows} == {"coding_focus", "daily_briefing"}
    assert "token" not in str(rows).lower()
