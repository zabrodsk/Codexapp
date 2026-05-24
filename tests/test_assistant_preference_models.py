import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_preference_models import active_coding_default_duration, active_email_duration_multiplier, update_preference_models


def _email_outcomes(count=5, predicted=30, actual=45):
    return [{"lane": "email_triage", "predicted_minutes": predicted, "actual_minutes": actual, "outcome_status": "observed"} for _ in range(count)]


def _coding_outcomes(count=5, actual=110):
    return [{"lane": "coding_focus", "booked_minutes": 90, "actual_minutes": actual, "outcome_status": "observed"} for _ in range(count)]


def test_email_duration_multiplier_requires_evidence_and_is_bounded(tmp_path):
    low = update_preference_models(db_path=tmp_path / "learning.sqlite3", outcomes=_email_outcomes(count=4), live=False)
    assert low["models"][0]["status"] == "insufficient_evidence"
    payload = update_preference_models(db_path=tmp_path / "learning.sqlite3", outcomes=_email_outcomes(count=5, predicted=30, actual=90), live=True)
    model = [m for m in payload["models"] if m["preference_key"] == "email_triage.duration_multiplier"][0]
    assert model["status"] == "active_bounded"
    assert model["value"] == 1.25
    assert active_email_duration_multiplier(db_path=tmp_path / "learning.sqlite3")["value"] == 1.25


def test_coding_default_duration_requires_evidence_and_is_bounded(tmp_path):
    payload = update_preference_models(db_path=tmp_path / "learning.sqlite3", outcomes=_coding_outcomes(count=5, actual=150), live=True)
    model = [m for m in payload["models"] if m["preference_key"] == "coding_focus.default_duration_minutes"][0]
    assert model["status"] == "active_bounded"
    assert model["value"] == 120
    assert active_coding_default_duration(db_path=tmp_path / "learning.sqlite3")["value"] == 120


def test_training_and_reminder_learning_are_proposals_only(tmp_path):
    outcomes = [{"lane": "training", "outcome_status": "observed"}, {"lane": "task_reminder", "outcome_status": "observed"}]
    payload = update_preference_models(db_path=tmp_path / "learning.sqlite3", outcomes=outcomes, live=True)
    assert payload["proposal_count"] >= 2
    assert all(item["proposal_type"] == "review_required" for item in payload["proposals"])
    assert not any(item.get("status") == "active_bounded" and item["preference_key"].startswith("training") for item in payload["models"])
