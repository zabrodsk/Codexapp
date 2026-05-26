import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_run_lock import acquire_run_lock
from assistant_scheduler_state import AssistantSchedulerState
from email_triage_scheduler import WORKFLOW, run_email_triage_scheduler, scheduler_idempotency_key


def _helper_payload(attention=True):
    return {
        "status": "ok",
        "messages": [{"message_id": "msg-1", "subject": "Secret"}],
        "evaluations": [{"message_id": "msg-1", "important": attention, "priority": "urgent"}],
    }


def _paths(tmp_path):
    return {
        "scheduler_db_path": tmp_path / "assistant_scheduler.sqlite3",
        "calendar_state_db_path": tmp_path / "assistant_calendar.sqlite3",
        "ledger_path": tmp_path / "assistant_audit.jsonl",
        "state_file": tmp_path / "email_triage_scheduler.json",
    }


def test_scheduler_without_live_is_dry_run_only(tmp_path):
    with patch("email_triage_scheduler.book_email_triage_proposal") as book:
        payload = run_email_triage_scheduler(
            planning_date="2026-05-25",
            helper_payload=_helper_payload(),
            existing_events=[],
            live=False,
            now_local="2026-05-25T09:30:00+02:00",
            **_paths(tmp_path),
        )

    assert payload["status"] == "dry_run_proposal"
    assert payload["calendar_write_attempted"] is False
    book.assert_not_called()


def test_scheduler_lock_prevents_overlap(tmp_path):
    key = scheduler_idempotency_key(planning_day=date(2026, 5, 25))
    acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=key,
        ttl_seconds=600,
        db_path=tmp_path / "assistant_scheduler.sqlite3",
        write_audit=False,
    )

    payload = run_email_triage_scheduler(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[],
        live=True,
        now_local="2026-05-25T09:30:00+02:00",
        **_paths(tmp_path),
    )

    assert payload["status"] == "skipped_duplicate_run"
    assert payload["calendar_write_attempted"] is False


def test_weekend_scheduler_skips_before_mail_read(tmp_path):
    with patch("email_triage_scheduler.build_email_triage_proposals") as proposals:
        payload = run_email_triage_scheduler(
            planning_date="2026-05-23",
            live=True,
            now_local="2026-05-23T09:30:00+02:00",
            **_paths(tmp_path),
        )

    assert payload["status"] == "skipped_weekend_target"
    assert payload["calendar_write_attempted"] is False
    proposals.assert_not_called()


def test_no_attention_emails_clean_skip(tmp_path):
    payload = run_email_triage_scheduler(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(attention=False),
        existing_events=[],
        live=True,
        health_payload={"status": "ok"},
        now_local="2026-05-25T09:30:00+02:00",
        **_paths(tmp_path),
    )

    assert payload["status"] == "skipped_no_attention_emails"
    assert payload["calendar_write_attempted"] is False


def test_calendar_health_failure_dead_letters(tmp_path):
    payload = run_email_triage_scheduler(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[],
        live=True,
        health_payload={"status": "blocked", "blocked_checks": ["eventkit"]},
        now_local="2026-05-25T09:30:00+02:00",
        **_paths(tmp_path),
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "calendar_write_health_not_ok"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "calendar_write_health_not_ok"


def test_writer_failure_dead_letters_and_notification_dry_run(tmp_path):
    with patch(
        "email_triage_scheduler.book_email_triage_proposal",
        return_value={
            "status": "failed",
            "reason": "osascript_create_failed",
            "idempotency_key": "rocky:email:test",
            "calendar_write_attempted": True,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        },
    ):
        payload = run_email_triage_scheduler(
            planning_date="2026-05-25",
            helper_payload=_helper_payload(),
            existing_events=[],
            live=True,
            notify_failures=True,
            notification_dry_run=True,
            health_payload={"status": "ok"},
            now_local="2026-05-25T09:30:00+02:00",
            **_paths(tmp_path),
        )

    assert payload["status"] == "failed"
    assert payload["notification"]["status"] == "dry_run"
    rendered = json.dumps(payload)
    assert "Secret" not in rendered


def test_scheduler_passes_proposal_snapshot_to_live_booking(tmp_path):
    with patch(
        "email_triage_scheduler.book_email_triage_proposal",
        return_value={
            "status": "created",
            "reason": None,
            "idempotency_key": "rocky:email:test",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as book:
        payload = run_email_triage_scheduler(
            planning_date="2026-05-25",
            helper_payload=_helper_payload(),
            existing_events=[],
            live=True,
            health_payload={"status": "ok"},
            now_local="2026-05-25T09:30:00+02:00",
            **_paths(tmp_path),
        )

    assert payload["status"] == "created"
    book.assert_called_once()
    assert book.call_args.kwargs["proposal_payload"]["status"] == "proposal"
    assert book.call_args.kwargs["proposal_payload"]["idempotency_key"] == payload["idempotency_key"]
