import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_calibration_review import build_assistant_learning_calibration_review
from assistant_learning_store import AssistantLearningStore


def test_calibration_review_reports_pending_bounded_models_and_review_only_proposals(tmp_path):
    db = tmp_path / "learning.sqlite3"
    store = AssistantLearningStore(db)
    store.record_outcome({"lane": "email_triage", "outcome_type": "calendar_block_lifecycle", "source_ref": "email:1", "outcome_status": "succeeded", "confidence": 0.75, "safe_summary": "email triage kept"})
    store.record_outcome({"lane": "coding_focus", "outcome_type": "scheduler_run", "source_ref": "coding:1", "outcome_status": "succeeded", "confidence": 0.75, "safe_summary": "coding run succeeded"})
    store.record_outcome({"lane": "training", "outcome_type": "calendar_block_lifecycle", "source_ref": "training:1", "outcome_status": "active", "confidence": 0.65, "safe_summary": "training block active"})
    store.upsert_preference_model({"preference_key": "email_triage.duration_multiplier", "lane": "email_triage", "status": "insufficient_evidence", "value": 1.0, "confidence": 0.5, "evidence_count": 1, "reason": "insufficient_email_duration_evidence", "bounds": {"min": 0.75, "max": 1.25}, "model": {"raw": "auth token secret"}})
    store.upsert_preference_model({"preference_key": "coding_focus.default_duration_minutes", "lane": "coding_focus", "status": "active_bounded", "value": 90, "confidence": 0.75, "evidence_count": 5, "reason": "bounded_coding_focus_default_duration", "bounds": {"min": 60, "max": 120}})
    store.create_learning_proposal({"preference_key": "training.preferred_start_time", "proposal_type": "review_required", "status": "proposed", "reason": "training_timing_observed_review_required", "confidence": 0.65, "evidence_count": 3, "proposal": {"auto_apply_allowed": False, "raw_notes": "secret token body"}})

    payload = build_assistant_learning_calibration_review(learning_db_path=db)

    assert payload["status"] == "ok"
    assert payload["active_bounded_count"] == 1
    assert {lane["lane"] for lane in payload["lanes"]} >= {"email_triage", "coding_focus", "training"}
    pending = next(item for item in payload["bounded_preferences"] if item["preference_key"] == "email_triage.duration_multiplier")
    assert pending["activation"] == "pending"
    assert "more_evidence" in pending["needed_for_activation"]
    assert payload["review_only_proposals"][0]["auto_apply_allowed"] is False
    assert "secret token" not in str(payload).lower()
    assert payload["calendar_write_attempted"] is False


def test_calibration_review_missing_store_is_calibration_pending(tmp_path):
    payload = build_assistant_learning_calibration_review(learning_db_path=tmp_path / "missing.sqlite3")

    assert payload["status"] == "calibration_pending"
    assert payload["reason"] == "assistant_learning_store_missing"
    assert payload["calendar_write_attempted"] is False
