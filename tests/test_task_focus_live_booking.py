import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_focus_live_booking import book_task_focus_proposal
from task_focus_proposal_engine import build_task_focus_proposals


def _task():
    return {
        "page_id": "page-1",
        "title": "Review investor follow-up",
        "priority": "High",
        "confidence": 0.9,
        "requires_dusan_action": True,
        "estimated_effort_minutes": 45,
        "calendar_block_status": "None",
        "source_ref": "apple-mail:message:abc",
        "dedupe_key": "task:abc",
    }


def test_task_focus_book_refuses_without_live():
    payload = book_task_focus_proposal(idempotency_key="rocky:task", live=False)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False


def test_task_focus_book_calls_calendar_writer_for_selected_proposal():
    proposal = build_task_focus_proposals(
        planning_date="2026-05-25",
        tasks=[_task()],
        existing_events=[],
        write_audit=False,
    )

    with patch(
        "task_focus_live_booking.create_calendar_block",
        return_value={
            "status": "created",
            "audit_id": "audit",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
        },
    ) as create:
        payload = book_task_focus_proposal(
            idempotency_key=proposal["idempotency_key"],
            planning_date="2026-05-25",
            tasks=[_task()],
            existing_events=[],
            live=True,
        )

    assert payload["status"] == "created"
    assert payload["calendar_write_attempted"] is True
    create.assert_called_once()
    assert create.call_args.kwargs["kind"] == "task_focus"
