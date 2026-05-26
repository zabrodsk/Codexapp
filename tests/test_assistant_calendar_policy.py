import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_policy import evaluate_calendar_policy, stable_idempotency_key


def test_monday_through_thursday_booking_is_allowed():
    decision = evaluate_calendar_policy(
        kind="training",
        day="2026-05-25",
        start="08:00",
        duration_minutes=90,
        source_refs=["trainingpeaks:test"],
        label="Endurance",
    )

    assert decision.allowed is True
    assert decision.decision == "allowed"
    assert decision.title == "Rocky: Training - Endurance"
    assert "policy_allowed" in decision.reasons


def test_friday_saturday_sunday_bookings_are_blocked():
    for day in ("2026-05-22", "2026-05-23", "2026-05-24"):
        decision = evaluate_calendar_policy(
            kind="email_triage",
            day=day,
            start="13:00",
            duration_minutes=30,
        )
        assert decision.allowed is False
        assert "proactive_booking_blocked_on_friday_saturday_sunday" in decision.reasons


def test_coding_focus_requires_at_least_one_hour_and_ends_by_1930():
    too_short = evaluate_calendar_policy(
        kind="coding_focus",
        day="2026-05-25",
        start="13:00",
        duration_minutes=45,
    )
    too_late = evaluate_calendar_policy(
        kind="coding_focus",
        day="2026-05-25",
        start="19:00",
        duration_minutes=60,
    )

    assert too_short.allowed is False
    assert "coding_focus_minimum_duration_is_60_minutes" in too_short.reasons
    assert too_late.allowed is False
    assert "coding_focus_must_end_by_19_30" in too_late.reasons


def test_email_triage_allows_short_chunks_and_still_requires_post_noon_start():
    short_chunk = evaluate_calendar_policy(
        kind="email_triage",
        day="2026-05-25",
        start="13:00",
        duration_minutes=15,
    )
    too_early = evaluate_calendar_policy(
        kind="email_triage",
        day="2026-05-25",
        start="11:30",
        duration_minutes=30,
    )

    assert short_chunk.allowed is True
    assert "policy_allowed" in short_chunk.reasons
    assert too_early.allowed is False
    assert "email_triage_must_start_no_earlier_than_noon" in too_early.reasons


def test_idempotency_key_is_stable_for_same_inputs():
    first = stable_idempotency_key(
        kind="task_focus",
        day="2026-05-25",
        start="15:00",
        duration_minutes=60,
        source_refs=["notion:abc"],
    )
    second = stable_idempotency_key(
        kind="task_focus",
        day="2026-05-25",
        start="15:00",
        duration_minutes=60,
        source_refs=["notion:abc"],
    )

    assert first == second
    assert first.startswith("rocky:task_focus:2026-05-25:")
