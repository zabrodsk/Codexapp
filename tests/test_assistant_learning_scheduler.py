import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_scheduler import run_assistant_learning_scheduler


def test_learning_scheduler_dry_run_does_not_write_learning_db(tmp_path):
    db = tmp_path / "learning.sqlite3"
    payload = run_assistant_learning_scheduler(planning_date="2026-05-25", live=False, learning_db_path=db, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", outcomes_payload={"status": "ok", "outcome_count": 0, "outcomes": []})
    assert payload["status"] == "skipped_insufficient_evidence"
    assert not db.exists()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["calendar_write_attempted"] is False


def test_learning_scheduler_live_reports_calibration_pending_with_insufficient_evidence(tmp_path):
    db = tmp_path / "learning.sqlite3"
    outcomes = {"status": "ok", "outcome_count": 1, "outcomes": [{"lane": "email_triage", "predicted_minutes": 30, "actual_minutes": 45}]}
    payload = run_assistant_learning_scheduler(planning_date="2026-05-25", live=True, learning_db_path=db, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", outcomes_payload=outcomes)
    assert payload["status"] == "calibration_pending"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["last_status"] == "calibration_pending"


def test_learning_scheduler_live_updates_models_with_enough_evidence(tmp_path):
    db = tmp_path / "learning.sqlite3"
    outcomes = {"status": "ok", "outcome_count": 5, "outcomes": [{"lane": "email_triage", "predicted_minutes": 30, "actual_minutes": 45} for _ in range(5)]}
    payload = run_assistant_learning_scheduler(planning_date="2026-05-25", live=True, learning_db_path=db, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", outcomes_payload=outcomes)
    assert payload["status"] == "ok"
    assert payload["summary"]["active_bounded_count"] >= 1
    assert payload["calendar_write_attempted"] is False


def test_learning_scheduler_failure_creates_safe_dead_letter(tmp_path):
    payload = run_assistant_learning_scheduler(planning_date="2026-05-25", live=True, scheduler_db_path=tmp_path / "scheduler.sqlite3", ledger_path=tmp_path / "audit.jsonl", state_file=tmp_path / "state.json", outcomes_payload={"status": "blocked", "reason": "token secret failure"})
    assert payload["status"] == "failed"
    assert "dead_letter" in payload
    assert "token secret" not in str(payload)
