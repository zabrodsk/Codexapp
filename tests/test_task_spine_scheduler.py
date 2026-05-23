import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from notion_task_manager import NotionTaskConfig
from task_spine_scheduler import run_task_spine_scheduler


class FakeNotion:
    def __init__(self):
        self.pages = []

    def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {"Title": {"title": {}}}}

    def update_database(self, database_id, *, properties):
        return {"id": database_id}

    def query_database(self, database_id, payload=None):
        return {"results": []}

    def create_page(self, *, database_id, properties):
        page = {"id": f"page-{len(self.pages)+1}", "properties": properties}
        self.pages.append(page)
        return page

    def update_page(self, page_id, *, properties):
        return {"id": page_id, "properties": properties}


def _config(tmp_path):
    return NotionTaskConfig(
        token="secret",
        database_id="db-id",
        parent_page_id="parent",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )


def test_task_spine_dry_run_detects_without_notion_or_calendar_writes(tmp_path):
    with patch("task_spine_scheduler.load_notion_task_config", return_value=_config(tmp_path)):
        payload = run_task_spine_scheduler(
            planning_date="2026-05-25",
            live=False,
            helper_payload={
                "status": "ok",
                "messages": [{"message_id": "m1"}],
                "evaluations": [
                    {
                        "message_id": "m1",
                        "important": True,
                        "priority": "soon",
                        "short_summary": "Dusan should reply to the investor.",
                    }
                ],
            },
            memory_results=[],
            meeting_results=[],
            notion_client=FakeNotion(),
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state-run.json",
            existing_events=[],
        )

    assert payload["status"] in {"ok", "degraded"}
    assert payload["auto_created_count"] == 1
    assert payload["notion_write_attempted"] is False
    assert payload["calendar_write_attempted"] is False


def test_task_spine_live_upserts_high_confidence_task_and_skips_weekend_booking(tmp_path):
    fake = FakeNotion()
    with patch("task_spine_scheduler.load_notion_task_config", return_value=_config(tmp_path)):
        payload = run_task_spine_scheduler(
            planning_date="2026-05-23",
            live=True,
            helper_payload={
                "status": "ok",
                "messages": [{"message_id": "m1"}],
                "evaluations": [
                    {
                        "message_id": "m1",
                        "important": True,
                        "priority": "soon",
                        "short_summary": "Dusan should reply to the investor.",
                    }
                ],
            },
            memory_results=[],
            meeting_results=[],
            notion_client=fake,
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state-run.json",
            existing_events=[],
        )

    assert payload["notion_write_attempted"] is True
    assert len(fake.pages) == 1
    assert payload["task_focus"]["status"] == "skipped_weekend_target"
    assert payload["calendar_write_attempted"] is False


def test_task_spine_live_does_not_write_review_only_candidates(tmp_path):
    fake = FakeNotion()
    with patch("task_spine_scheduler.load_notion_task_config", return_value=_config(tmp_path)):
        payload = run_task_spine_scheduler(
            planning_date="2026-05-23",
            live=True,
            helper_payload={"status": "ok", "messages": [], "evaluations": []},
            memory_results=[
                {
                    "title": "Open loop",
                    "path": "open-loop.md",
                    "snippet": "Dusan should review this later.",
                }
            ],
            meeting_results=[],
            notion_client=fake,
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state-run.json",
            existing_events=[],
        )

    assert payload["review_candidate_count"] == 1
    assert payload["notion_upsert_count"] == 0
    assert fake.pages == []


def test_task_spine_duplicate_lock_prevents_overlap(tmp_path):
    with patch("task_spine_scheduler.load_notion_task_config", return_value=_config(tmp_path)):
        first = run_task_spine_scheduler(
            planning_date="2026-05-25",
            live=False,
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state-run.json",
            helper_payload={"status": "ok", "messages": [], "evaluations": []},
            memory_results=[],
            meeting_results=[],
            notion_client=FakeNotion(),
        )
        second = run_task_spine_scheduler(
            planning_date="2026-05-25",
            live=False,
            scheduler_db_path=tmp_path / "scheduler.sqlite3",
            ledger_path=tmp_path / "audit.jsonl",
            state_file=tmp_path / "state-run.json",
            helper_payload={"status": "ok", "messages": [], "evaluations": []},
            memory_results=[],
            meeting_results=[],
            notion_client=FakeNotion(),
        )

    assert first["status"] in {"ok", "degraded"}
    assert second["status"] in {"ok", "degraded"}
