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
