import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_reminder_engine import run_task_reminders


def test_task_reminders_select_due_tasks_without_notification_by_default():
    payload = run_task_reminders(
        today="2026-05-25",
        tasks=[
            {
                "title": "Review deck",
                "status": "Open",
                "priority": "High",
                "requires_dusan_action": True,
                "next_reminder_date": "2026-05-25",
            }
        ],
        notify=False,
    )

    assert payload["status"] == "ok"
    assert payload["reminder_count"] == 1
    assert "notification" not in payload


def test_task_reminders_notify_when_requested():
    with patch(
        "task_reminder_engine.dispatch_failure_notification",
        return_value={"status": "dry_run", "notification_attempted": False},
    ) as dispatch:
        payload = run_task_reminders(
            today="2026-05-25",
            tasks=[{"title": "Review deck", "status": "Open", "priority": "High", "requires_dusan_action": True}],
            notify=True,
            notification_dry_run=True,
        )

    assert payload["notification"]["status"] == "dry_run"
    dispatch.assert_called_once()
