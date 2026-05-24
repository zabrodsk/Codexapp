import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from daily_personal_signal_collector import collect_daily_personal_signals
from assistant_learning_store import AssistantLearningStore


def test_collects_sanitized_lane_snapshot_without_mutation(tmp_path):
    payload = collect_daily_personal_signals(
        planning_date="2026-05-25",
        calendar_events=[
            {
                "summary": "Investor meeting",
                "start_local": "2026-05-25 10:00:00",
                "end_local": "2026-05-25 11:00:00",
                "all_day": False,
                "calendar": "Calendar",
                "location": "Office",
                "description": "private token secret raw notes",
            },
            {
                "summary": "Rocky: Training - Run",
                "start_local": "2026-05-25 08:00:00",
                "end_local": "2026-05-25 10:30:00",
                "all_day": False,
                "calendar": "Calendar",
                "description": "Booked by: Rocky",
            },
        ],
        scheduler_states={"email_triage_booking": {"last_status": "failed", "reason": "calendar_write_health_not_ok"}},
        email_payload={
            "status": "proposal",
            "email_attention": {"attention_count": 3, "unread_count": 9, "priority_buckets": {"high": 1}},
            "estimate": {"estimated_minutes": 45},
            "idempotency_key": "rocky:email:1",
        },
        task_payload={
            "status": "ok",
            "tasks": [
                {"title": "Call Jana", "priority": "Urgent", "due_date": "2026-05-25", "confidence": 0.9, "requires_dusan_action": True},
                {"title": "Read background", "priority": "Low", "confidence": 0.7, "requires_dusan_action": True},
            ],
        },
        coding_payload={
            "status": "ok",
            "work_item_count": 1,
            "selected_count": 1,
            "selected_focus_items": [
                {"project": "Rocky", "title": "Finish daily brief", "confidence": 0.86, "priority": "High", "work_item_id": "coding:1"}
            ],
        },
        coding_proposals_payload={
            "status": "proposal",
            "proposals": [{"status": "proposal", "idempotency_key": "rocky:coding:1", "selected_work_item": {"project": "Rocky"}}],
            "idempotency_keys": ["rocky:coding:1"],
        },
        task_focus_payload={"status": "proposal", "idempotency_key": "rocky:task:1"},
        command_payload={"status": "ok", "commands": [{"source_ref": "discord:1", "status": "applied", "text_preview": "Add task"}]},
        dead_letters=[{"job_name": "email_triage_booking", "failure_class": "calendar_write_health_not_ok"}],
    )

    assert payload["status"] == "ok"
    assert payload["booking_allowed_today"] is True
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    assert payload["calendar"]["event_count"] == 2
    assert "description" not in payload["calendar"]["events"][0]
    assert payload["training"]["today_block_count"] == 1
    assert payload["email"]["attention_count"] == 3
    assert payload["tasks"]["urgent_count"] == 1
    assert payload["coding"]["selected_count"] == 1
    assert payload["dead_letters"]["open_count"] == 1
    assert "token" not in str(payload).lower()


def test_friday_briefing_is_not_booking_allowed():
    payload = collect_daily_personal_signals(
        planning_date="2026-05-29",
        calendar_events=[],
        email_payload={"status": "proposal", "email_attention": {"attention_count": 1}, "estimate": {"estimated_minutes": 30}},
        task_payload={"status": "ok", "tasks": []},
        coding_payload={"status": "ok", "selected_focus_items": []},
        command_payload={"status": "ok", "commands": []},
        dead_letters=[],
    )

    assert payload["is_weekday_briefing_day"] is True
    assert payload["booking_allowed_today"] is False
    assert payload["booking_policy_reason"] == "proactive_booking_blocked_on_friday_saturday_sunday"


def test_daily_signals_include_learning_summary(tmp_path):
    store = AssistantLearningStore(tmp_path / "learning.sqlite3")
    store.record_outcome({"lane": "email_triage", "outcome_type": "duration", "source_ref": "x", "outcome_status": "observed", "safe_summary": "observed"})
    payload = collect_daily_personal_signals(
        planning_date="2026-05-25",
        calendar_events=[],
        scheduler_states={},
        email_payload={},
        task_payload={"status": "ok", "tasks": []},
        coding_payload={"status": "ok", "selected_focus_items": [], "work_items": []},
        coding_proposals_payload={"status": "skipped_no_coding_focus", "proposals": []},
        task_focus_payload={},
        command_payload={"commands": []},
        dead_letters=[],
        learning_db_path=tmp_path / "learning.sqlite3",
    )

    assert payload["learning"]["outcome_count"] == 1
    assert payload["calendar_write_attempted"] is False
