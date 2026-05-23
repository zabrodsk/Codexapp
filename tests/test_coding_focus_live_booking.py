import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_focus_live_booking import book_coding_focus_proposal
from coding_focus_proposal_engine import build_coding_focus_proposals


def _item():
    return {
        "work_item_id": "coding-work:1",
        "project": "Matchbook",
        "title": "Finish go-live smoke",
        "status": "active",
        "priority": "High",
        "estimated_effort_minutes": 90,
        "confidence": 0.86,
        "requires_dusan_decision": False,
        "source_refs": ["codex:session:1"],
        "where_left_off": "Tests passed",
        "recommended_next_step": "Run smoke",
    }


def test_coding_focus_book_requires_live():
    payload = book_coding_focus_proposal(idempotency_key="rocky:coding:test", live=False)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False


def test_coding_focus_book_calls_calendar_writer_with_focus_metadata():
    proposals = build_coding_focus_proposals(planning_date="2026-05-25", work_items=[_item()], existing_events=[], write_audit=False)
    key = proposals["proposals"][0]["idempotency_key"]

    with patch("coding_focus_live_booking.create_calendar_block", return_value={"status": "created", "audit_id": "audit-1", "calendar_write_attempted": True, "calendar_event_created": True}) as writer:
        payload = book_coding_focus_proposal(idempotency_key=key, planning_date="2026-05-25", live=True, work_items=[_item()], existing_events=[])

    assert payload["status"] == "created"
    kwargs = writer.call_args.kwargs
    assert kwargs["kind"] == "coding_focus"
    assert kwargs["metadata_extra"]["where_you_left_off"] == "Tests passed"
    assert kwargs["metadata_extra"]["recommended_next_step"] == "Run smoke"
