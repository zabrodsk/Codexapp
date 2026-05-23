import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_lifecycle_engine import next_reminder_date_for, run_task_lifecycle


class FakeNotion:
    def __init__(self):
        self.updated_pages = []

    def update_page(self, page_id, *, properties):
        self.updated_pages.append({"id": page_id, "properties": properties})
        return {"id": page_id, "properties": properties}


def _task(status="Open", priority="High"):
    return {
        "page_id": "page-1",
        "title": "Review investor note",
        "status": status,
        "priority": priority,
        "requires_dusan_action": True,
        "next_reminder_date": "2026-05-25",
        "reminder_count": 0,
    }


def test_next_reminder_cadence_skips_weekends():
    assert next_reminder_date_for(_task(priority="High"), today="2026-05-28") == "2026-06-01"
    assert next_reminder_date_for(_task(priority="Normal"), today="2026-05-28") == "2026-06-02"
    assert next_reminder_date_for(_task(priority="Low"), today="2026-05-28") == "2026-06-08"


def test_lifecycle_dry_run_does_not_update_notion():
    fake = FakeNotion()

    payload = run_task_lifecycle(today="2026-05-25", tasks=[_task()], live=False, client=fake)

    assert payload["status"] == "dry_run"
    assert payload["would_update_count"] == 1
    assert fake.updated_pages == []
    assert payload["notion_write_attempted"] is False


def test_lifecycle_live_updates_reminder_metadata():
    fake = FakeNotion()

    payload = run_task_lifecycle(today="2026-05-25", tasks=[_task()], live=True, client=fake)

    assert payload["status"] == "updated"
    assert payload["updated_count"] == 1
    assert fake.updated_pages
    encoded = json.dumps(fake.updated_pages[0])
    assert "Last reminded date" in encoded
    assert "Next reminder date" in encoded
    assert payload["notion_write_attempted"] is True


def test_lifecycle_ignores_terminal_tasks():
    fake = FakeNotion()

    payload = run_task_lifecycle(today="2026-05-25", tasks=[_task(status="Done")], live=True, client=fake)

    assert payload["status"] == "skipped_no_due_tasks"
    assert payload["terminal_skipped_count"] == 1
    assert fake.updated_pages == []
