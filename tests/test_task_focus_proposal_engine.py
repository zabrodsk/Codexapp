import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_focus_proposal_engine import build_task_focus_proposals


def _task(**overrides):
    task = {
        "page_id": "page-1",
        "rocky_task_id": "rocky-task:1",
        "title": "Review investor follow-up",
        "priority": "High",
        "confidence": 0.9,
        "requires_dusan_action": True,
        "estimated_effort_minutes": 45,
        "calendar_block_status": "None",
        "source_ref": "apple-mail:message:abc",
        "dedupe_key": "task:abc",
    }
    task.update(overrides)
    return task


def test_task_focus_proposal_uses_calendar_dry_run_for_weekday():
    payload = build_task_focus_proposals(
        planning_date="2026-05-25",
        tasks=[_task()],
        existing_events=[],
        write_audit=False,
    )

    assert payload["status"] == "proposal"
    assert payload["calendar_write_attempted"] is False
    assert payload["idempotency_key"].startswith("rocky:task_focus:2026-05-25:")
    assert payload["selected_proposal"]["duration_minutes"] == 45


def test_task_focus_proposal_blocks_weekend():
    payload = build_task_focus_proposals(
        planning_date="2026-05-23",
        tasks=[_task()],
        existing_events=[],
        write_audit=False,
    )

    assert payload["status"] == "skipped_weekend_target"
    assert payload["calendar_write_attempted"] is False


def test_task_focus_existing_same_day_block_skips_duplicate():
    payload = build_task_focus_proposals(
        planning_date="2026-05-25",
        tasks=[_task()],
        existing_events=[
            {
                "summary": "Rocky: Task focus - Review investor follow-up",
                "description": "Dedupe key: task:abc",
                "start_local": "2026-05-25 15:00:00",
                "end_local": "2026-05-25 16:00:00",
                "all_day": False,
            }
        ],
        write_audit=False,
    )

    assert payload["status"] == "skipped_duplicate"
