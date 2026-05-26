import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from email_triage_proposal_engine import build_email_triage_proposals


def _helper_payload(attention=True):
    evaluations = [
        {"message_id": "msg-1", "important": attention, "priority": "urgent"},
        {"message_id": "msg-2", "important": False, "priority": "ignore"},
    ]
    return {
        "status": "ok",
        "messages": [
            {"message_id": "msg-1", "subject": "Secret subject", "body_excerpt": "secret body"},
            {"message_id": "msg-2", "subject": "Other"},
        ],
        "evaluations": evaluations,
    }


def _heavy_helper_payload():
    evaluations = [
        {"message_id": f"msg-{idx}", "important": True, "priority": "urgent"}
        for idx in range(1, 5)
    ]
    return {
        "status": "ok",
        "messages": [{"message_id": item["message_id"], "subject": "Hidden"} for item in evaluations],
        "evaluations": evaluations,
    }


def test_monday_same_day_proposal_uses_early_afternoon_window(tmp_path):
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
        now_local="2026-05-25T09:30:00+02:00",
    )

    proposal = payload["selected_proposal"]
    rendered = json.dumps(payload)
    assert payload["status"] == "proposal"
    assert proposal["proposal"]["start"] == "2026-05-25T13:00:00+02:00"
    assert proposal["duration_minutes"] == 30
    assert payload["idempotency_key"].startswith("rocky:email_triage:2026-05-25:")
    assert "Secret subject" not in rendered
    assert "secret body" not in rendered


def test_weekend_proposal_is_blocked_before_mail_read():
    payload = build_email_triage_proposals(
        planning_date="2026-05-23",
        helper_payload=_helper_payload(),
        existing_events=[],
        now_local="2026-05-23T09:30:00+02:00",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "proactive_booking_blocked_on_friday_saturday_sunday"
    assert "email_attention" not in payload


def test_future_date_email_triage_is_blocked():
    payload = build_email_triage_proposals(
        planning_date="2026-05-26",
        helper_payload=_helper_payload(),
        existing_events=[],
        now_local="2026-05-25T09:30:00+02:00",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "email_triage_must_be_same_day"


def test_no_attention_emails_cleanly_skip():
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(attention=False),
        existing_events=[],
        now_local="2026-05-25T09:30:00+02:00",
    )

    assert payload["status"] == "skipped_no_attention_emails"
    assert payload["calendar_write_attempted"] is False


def test_existing_same_day_rocky_email_block_is_duplicate_skip():
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_helper_payload(),
        existing_events=[
            {
                "summary": "Rocky: Email triage - unread attention",
                "description": "Booked by: Rocky",
                "start_local": "2026-05-25 16:00:00",
                "end_local": "2026-05-25 16:30:00",
                "calendar": "Calendar",
                "all_day": False,
            }
        ],
        now_local="2026-05-25T09:30:00+02:00",
    )

    assert payload["status"] == "skipped_duplicate"
    assert payload["reason"] == "duplicate_rocky_block"


def test_no_full_slot_falls_back_to_30_minute_split(tmp_path):
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_heavy_helper_payload(),
        existing_events=[
            {
                "summary": "Busy",
                "description": "",
                "start_local": "2026-05-25 12:30:00",
                "end_local": "2026-05-25 17:30:00",
                "calendar": "Calendar",
                "all_day": False,
            }
        ],
        ledger_path=tmp_path / "assistant_audit.jsonl",
        now_local="2026-05-25T09:30:00+02:00",
    )

    proposal = payload["selected_proposal"]
    assert payload["status"] == "proposal"
    assert proposal["split_recovery"] is True
    assert proposal["original_duration_minutes"] == 60
    assert proposal["duration_minutes"] == 30
    assert proposal["proposal"]["start"] == "2026-05-25T12:00:00+02:00"


def test_no_thirty_minute_slot_falls_back_to_15_minute_split(tmp_path):
    payload = build_email_triage_proposals(
        planning_date="2026-05-25",
        helper_payload=_heavy_helper_payload(),
        existing_events=[
            {
                "summary": "Busy",
                "description": "",
                "start_local": "2026-05-25 12:15:00",
                "end_local": "2026-05-25 17:30:00",
                "calendar": "Calendar",
                "all_day": False,
            }
        ],
        ledger_path=tmp_path / "assistant_audit.jsonl",
        now_local="2026-05-25T09:30:00+02:00",
    )

    proposal = payload["selected_proposal"]
    assert payload["status"] == "proposal"
    assert proposal["split_recovery"] is True
    assert proposal["original_duration_minutes"] == 60
    assert proposal["duration_minutes"] == 15
    assert proposal["proposal"]["start"] == "2026-05-25T12:00:00+02:00"
