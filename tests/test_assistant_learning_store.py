import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_store import AssistantLearningStore


def test_learning_store_records_sanitized_outcome(tmp_path):
    store = AssistantLearningStore(tmp_path / "learning.sqlite3")
    row = store.record_outcome({"lane": "email_triage", "outcome_type": "duration", "source_ref": "email:1", "predicted_minutes": 30, "actual_minutes": 45, "outcome_status": "observed", "confidence": 0.8, "evidence": {"raw_body": "secret token abc"}, "safe_summary": "Email triage observed"})
    assert row["lane"] == "email_triage"
    assert row["evidence"]["raw_body"]["redacted"] is True
    assert "token abc" not in str(row)


def test_learning_store_upserts_active_preference_and_proposal(tmp_path):
    store = AssistantLearningStore(tmp_path / "learning.sqlite3")
    pref = store.upsert_preference_model({"preference_key": "email_triage.duration_multiplier", "lane": "email_triage", "status": "active_bounded", "value": 1.1, "confidence": 0.75, "evidence_count": 5, "bounds": {"min": 0.75, "max": 1.25}, "reason": "bounded"})
    proposal = store.create_learning_proposal({"preference_key": "training.preferred_start_time", "proposal_type": "review_required", "reason": "training_timing_observed_review_required", "proposal": {"token_url": "https://example.test?token=secret"}})
    assert pref["status"] == "active_bounded"
    assert proposal["proposal"]["token_url"]["redacted"] is True
    assert store.mark_proposal_applied(proposal["proposal_id"])["status"] == "applied"
