import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_priority_arbitrator import arbitrate_daily_priorities


def _signals(**overrides):
    base = {
        "planning_date": "2026-05-25",
        "booking_allowed_today": True,
        "calendar": {"free_windows": [{"start": "12:30", "end": "14:30", "minutes": 120}], "event_count": 2},
        "training": {"today_block_count": 1, "summary": "Training protected 08:00-10:30."},
        "email": {"status": "proposal", "attention_count": 4, "estimated_minutes": 45, "repair_candidate": True, "idempotency_key": "rocky:email:1"},
        "tasks": {"urgent_count": 0, "due_soon_count": 0, "top_tasks": []},
        "coding": {"selected_count": 1, "top_items": [{"project": "Rocky", "title": "Daily brief", "confidence": 0.88, "priority": "High"}], "proposal_idempotency_keys": ["rocky:coding:1"]},
        "task_focus": {"status": "skipped_no_focus_tasks"},
        "dead_letters": {"open_count": 0, "items": []},
        "scheduler": {"problem_count": 0, "states": {}},
    }
    base.update(overrides)
    return base


def test_arbitrates_email_before_coding_when_attention_is_time_sensitive():
    result = arbitrate_daily_priorities(_signals())

    assert result["status"] == "ok"
    assert result["top_priority"]["category"] == "email"
    assert result["safe_booking_actions"][0]["action"] == "email_triage_repair"
    assert result["safe_booking_actions"][0]["idempotency_key"] == "rocky:email:1"


def test_high_confidence_coding_beats_routine_tasks():
    result = arbitrate_daily_priorities(
        _signals(
            email={"status": "skipped_no_attention_emails", "attention_count": 0, "estimated_minutes": 0},
            tasks={"urgent_count": 0, "due_soon_count": 1, "top_tasks": [{"title": "Routine admin", "priority": "Normal", "confidence": 0.8}]},
        )
    )

    assert result["top_priority"]["category"] == "coding"
    assert result["safe_booking_actions"][0]["action"] == "coding_focus_book"
    assert result["safe_booking_actions"][0]["idempotency_key"] == "rocky:coding:1"


def test_weekend_or_friday_booking_policy_blocks_actions():
    result = arbitrate_daily_priorities(_signals(planning_date="2026-05-29", booking_allowed_today=False))

    assert result["status"] == "ok"
    assert result["safe_booking_actions"] == []
    assert any(item["category"] == "policy" for item in result["blocked_or_risky"])


def test_llm_failure_falls_back_to_deterministic_arbitration():
    def broken(_prompt):
        raise RuntimeError("model unavailable token secret")

    result = arbitrate_daily_priorities(_signals(), use_llm=True, llm_func=broken)

    assert result["status"] == "degraded"
    assert result["llm"]["status"] == "degraded"
    assert result["top_priority"]["category"] == "email"
    assert "token" not in str(result).lower()
