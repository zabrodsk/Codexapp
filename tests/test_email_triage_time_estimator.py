import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from email_triage_time_estimator import estimate_email_triage_minutes


def test_no_attention_email_estimate_is_zero():
    estimate = estimate_email_triage_minutes({"attention_count": 0, "priority_buckets": {}})

    assert estimate["estimated_minutes"] == 0
    assert estimate["reason"] == "no_attention_emails"


def test_attention_email_estimate_has_minimum_and_rounding():
    estimate = estimate_email_triage_minutes(
        {"attention_count": 2, "priority_buckets": {"urgent": 1, "soon": 1}}
    )

    assert estimate["raw_estimated_minutes"] == 22
    assert estimate["estimated_minutes"] == 30
    assert estimate["minimum_minutes"] == 30


def test_attention_email_estimate_caps_at_90_minutes():
    estimate = estimate_email_triage_minutes(
        {"attention_count": 12, "priority_buckets": {"urgent": 12}}
    )

    assert estimate["estimated_minutes"] == 90
    assert estimate["maximum_minutes"] == 90


def test_email_estimate_applies_bounded_learning_multiplier():
    estimate = estimate_email_triage_minutes(
        {"attention_count": 8, "priority_buckets": {"soon": 4, "later": 4}},
        preferences={"email_triage.duration_multiplier": {"status": "active_bounded", "value": 1.25, "confidence": 0.8}},
    )

    assert estimate["raw_estimated_minutes"] == 64
    assert estimate["adjusted_raw_estimated_minutes"] == 80
    assert estimate["estimated_minutes"] == 90
    assert estimate["learning_preference"]["preference_key"] == "email_triage.duration_multiplier"
