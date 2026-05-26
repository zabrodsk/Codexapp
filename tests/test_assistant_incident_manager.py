import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_incident_manager import list_incidents, respond_to_incident, retry_incident, run_incident_manager
from assistant_scheduler_state import AssistantSchedulerState


def _state(tmp_path):
    return AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3")


def _open_dead_letter(tmp_path, *, job_name="email_triage_booking", workflow="email_triage_scheduler", failure_class="no_available_slot", summary="No available email triage slot", idempotency_key=None):
    return _state(tmp_path).upsert_dead_letter(
        job_name=job_name,
        workflow=workflow,
        idempotency_key=idempotency_key or f"{job_name}:{failure_class}",
        failure_class=failure_class,
        safe_summary=summary,
        source_refs=["test-source"],
        recovery_hint="test recovery",
        error_hash="abc123",
    )


def test_no_slot_email_triage_becomes_waiting_for_user_with_options(tmp_path):
    dead = _open_dead_letter(tmp_path)
    emails = []

    def poster(**kwargs):
        return {"status": "failed", "reason": "discord_http_403", "status_code": 403}

    def emailer(**kwargs):
        emails.append(kwargs)
        return {"status": "posted", "message_id": "email-1"}

    payload = run_incident_manager(
        live=True,
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
        state_file=tmp_path / "incident_state.json",
        quiet_minutes=0,
        post_func=poster,
        agentmail_send_func=emailer,
        now_local="2026-05-25T10:00:00+02:00",
    )

    assert payload["status"] == "manual_review_required"
    updated = _state(tmp_path).get_dead_letter(dead["dead_letter_id"])
    assert updated["status"] == "waiting_for_user"
    assert emails
    text = emails[0]["text"].lower()
    assert "split email triage" in text
    assert "15-30 minutes" in text
    assert "60-minute minimum applies only to coding focus" in text
    assert "book the next allowed monday-thursday slot" in text
    assert "skip email triage" in text


def test_stale_email_triage_no_slot_message_names_today_and_missed_date(tmp_path):
    _open_dead_letter(
        tmp_path,
        idempotency_key="email-triage-scheduler:2026-05-25",
        summary="no_available_slot",
    )
    emails = []

    def emailer(**kwargs):
        emails.append(kwargs)
        return {"status": "posted", "message_id": "email-1"}

    payload = run_incident_manager(
        live=True,
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
        state_file=tmp_path / "incident_state.json",
        quiet_minutes=0,
        post_func=lambda **kwargs: {"status": "failed", "reason": "discord_http_403", "status_code": 403},
        agentmail_send_func=emailer,
        now_local="2026-05-26T08:30:00+02:00",
    )

    assert payload["status"] == "manual_review_required"
    text = emails[0]["text"]
    assert "Rocky could not book email triage for 2026-05-25." in text
    assert "Today is 2026-05-26" in text
    assert "catch-up email triage" in text
    assert "15-30 minutes" in text
    assert "Skip/acknowledge the 2026-05-25 email triage incident" in text


def test_daily_notification_failure_can_be_retried_without_calendar_or_notion_writes(tmp_path, monkeypatch):
    dead = _open_dead_letter(
        tmp_path,
        job_name="daily_personal_briefing",
        workflow="daily_personal_briefing_scheduler",
        failure_class="daily_personal_briefing_notification_failed",
        summary="Daily notification failed for 2026-05-25",
    )

    def fake_daily(**kwargs):
        return {
            "briefing": {
                "discord_message": "Rocky daily brief - 2026-05-25\nToday\n- Safe summary",
                "message_hash": "hash1",
            }
        }

    monkeypatch.setattr("assistant_incident_manager.run_daily_personal_briefing", fake_daily)

    def poster(**kwargs):
        return {"status": "posted", "message_ids": ["m1"]}

    payload = retry_incident(
        dead_letter_id=dead["dead_letter_id"],
        live=True,
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
        post_func=poster,
    )

    assert payload["status"] == "recovered"
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
    updated = _state(tmp_path).get_dead_letter(dead["dead_letter_id"])
    assert updated["status"] == "recovered"


def test_incident_manager_does_not_spam_already_notified_quiet_window(tmp_path):
    dead = _open_dead_letter(tmp_path, failure_class="assistant_notification_failed", summary="Discord failed")
    _state(tmp_path).update_dead_letter_status(dead["dead_letter_id"], "notified")

    payload = run_incident_manager(
        live=True,
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
        state_file=tmp_path / "incident_state.json",
        quiet_minutes=9999,
    )

    assert payload["processed"][0]["action"] == "skipped_quiet_window"
    assert _state(tmp_path).get_dead_letter(dead["dead_letter_id"])["status"] == "notified"


def test_incident_recent_and_response_are_state_only(tmp_path):
    dead = _open_dead_letter(tmp_path, failure_class="assistant_notification_failed", summary="Discord failed")

    recent = list_incidents(limit=10, scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3")
    assert recent["count"] == 1
    assert recent["incidents"][0]["dead_letter_id"] == dead["dead_letter_id"]

    blocked = respond_to_incident(
        dead_letter_id=dead["dead_letter_id"],
        action="acknowledge",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )
    assert blocked["status"] == "blocked"

    payload = respond_to_incident(
        dead_letter_id=dead["dead_letter_id"],
        action="acknowledge",
        live=True,
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )
    assert payload["status"] == "acknowledged"
    rendered = json.dumps(payload)
    assert "calendar_write_attempted" in rendered
    assert _state(tmp_path).get_dead_letter(dead["dead_letter_id"])["status"] == "acknowledged"
