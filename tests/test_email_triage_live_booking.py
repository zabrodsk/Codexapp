import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from email_triage_live_booking import book_email_triage_proposal
from email_triage_proposal_engine import build_email_triage_proposals


def _helper_payload():
    return {
        "status": "ok",
        "messages": [{"message_id": "msg-1", "subject": "Secret"}],
        "evaluations": [{"message_id": "msg-1", "important": True, "priority": "urgent"}],
    }


def _proposal_key(tmp_path):
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
        now_local="2026-05-25T09:30:00+02:00",
    )
    return payload["idempotency_key"]


def test_email_triage_book_refuses_without_live(tmp_path):
    payload = book_email_triage_proposal(
        idempotency_key="rocky:email:test",
        planning_date="2026-05-25",
        live=False,
        now_local="2026-05-25T09:30:00+02:00",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False


def test_email_triage_book_blocks_before_morning(tmp_path):
    payload = book_email_triage_proposal(
        idempotency_key="rocky:email:test",
        planning_date="2026-05-25",
        live=True,
        now_local="2026-05-25T07:30:00+02:00",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "email_triage_not_before_morning"


def test_email_triage_book_blocks_future_date(tmp_path):
    payload = book_email_triage_proposal(
        idempotency_key="rocky:email:test",
        planning_date="2026-05-26",
        live=True,
        now_local="2026-05-25T09:30:00+02:00",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "email_triage_must_be_same_day"


def test_email_triage_book_calls_writer_for_selected_proposal(tmp_path):
    key = _proposal_key(tmp_path)
    with patch(
        "email_triage_live_booking.create_calendar_block",
        return_value={
            "status": "created",
            "reason": None,
            "audit_id": "audit-created",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as create:
        payload = book_email_triage_proposal(
            idempotency_key=key,
            planning_date="2026-05-25",
            live=True,
            helper_payload=_helper_payload(),
            existing_events=[],
            health_payload={"status": "ok"},
            ledger_path=tmp_path / "assistant_audit.jsonl",
            now_local="2026-05-25T09:30:00+02:00",
        )

    assert payload["status"] == "created"
    assert payload["calendar_write_attempted"] is True
    create.assert_called_once()
    assert create.call_args.kwargs["kind"] == "email_triage"
    assert create.call_args.kwargs["day"] == "2026-05-25"
    assert create.call_args.kwargs["window_start"] == "13:00"
    assert create.call_args.kwargs["metadata_extra"]["attention_count"] == 1


def test_email_triage_book_duplicate_skips_writer(tmp_path):
    duplicate_event = {
        "summary": "Rocky: Email triage - unread attention",
        "description": "Booked by: Rocky",
        "start_local": "2026-05-25 16:00:00",
        "end_local": "2026-05-25 16:30:00",
        "calendar": "Calendar",
        "all_day": False,
    }
    proposal = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[duplicate_event],
        now_local="2026-05-25T09:30:00+02:00",
    )
    with patch("email_triage_live_booking.create_calendar_block") as create:
        payload = book_email_triage_proposal(
            idempotency_key=proposal["idempotency_key"],
            planning_date="2026-05-25",
            live=True,
            helper_payload=_helper_payload(),
            existing_events=[duplicate_event],
            health_payload={"status": "ok"},
            now_local="2026-05-25T09:30:00+02:00",
        )

    assert payload["status"] == "skipped_duplicate"
    create.assert_not_called()
