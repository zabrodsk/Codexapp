import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_prep_notion_manager import (
    NotionMeetingPrepConfig,
    ensure_meeting_prep_database_schema,
    meeting_prep_to_properties,
    upsert_meeting_prep_note,
)


class FakeNotion:
    def __init__(self):
        self.databases = {}
        self.pages = []
        self.updated_pages = []

    def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {}}

    def update_database(self, database_id, *, properties):
        self.databases[database_id] = properties
        return {"id": database_id}

    def create_database(self, **kwargs):
        self.databases["db1"] = kwargs
        return {"id": "db1"}

    def query_database(self, database_id, payload=None):
        key = (((payload or {}).get("filter") or {}).get("rich_text") or {}).get("equals")
        return {"results": [page for page in self.pages if page.get("meeting_key") == key]}

    def create_page(self, *, database_id, properties):
        page = {"id": "page1", "meeting_key": properties["Meeting key"]["rich_text"][0]["text"]["content"]}
        self.pages.append(page)
        return page

    def update_page(self, page_id, *, properties):
        self.updated_pages.append((page_id, properties))
        return {"id": page_id}


def _config(tmp_path, database_id="db1"):
    return NotionMeetingPrepConfig(token="token", database_id=database_id, parent_page_id="parent", state_file=tmp_path / "state.json")


def test_schema_dry_run_does_not_write():
    payload = ensure_meeting_prep_database_schema(live=False, config=_config(Path("/tmp")))

    assert payload["status"] == "dry_run"
    assert payload["notion_write_attempted"] is False


def test_schema_live_failure_returns_blocked_payload(tmp_path):
    class FailingNotion(FakeNotion):
        def create_database(self, **kwargs):
            raise RuntimeError("notion_http_404:secret")

    payload = ensure_meeting_prep_database_schema(live=True, config=_config(tmp_path, database_id=None), client=FailingNotion())

    assert payload["status"] == "blocked"
    assert payload["reason"] == "notion_meeting_prep_schema_ensure_failed"
    assert "secret" not in str(payload)


def test_upsert_meeting_prep_note_creates_and_updates(tmp_path):
    fake = FakeNotion()
    meeting = {"meeting_key": "meeting:1", "title": "Jana update", "date": "2026-05-25", "calendar_event_ref": "calendar:1"}
    brief = {"status": "ok", "discord_message": "Prep\nFocus", "message_sha256": "hash", "confidence": 0.8, "source_refs": ["people/jana.md"], "brief": {"questions": ["What changed?"], "open_loops": ["Task"], "dusan_notes": ["Ask runway"]}}

    created = upsert_meeting_prep_note(meeting, brief, live=True, config=_config(tmp_path), client=fake)
    updated = upsert_meeting_prep_note(meeting, brief, live=True, config=_config(tmp_path), client=fake)

    assert created["status"] == "created"
    assert updated["status"] == "updated"
    assert created["notion_write_attempted"] is True


def test_properties_redact_secret_bearing_text():
    props = meeting_prep_to_properties(
        {"meeting_key": "meeting:1", "title": "Meeting token=secret"},
        {"status": "ok", "discord_message": "Bearer sk-test should redact", "source_refs": []},
        discord_status="Not sent",
    )

    assert "token=secret" not in str(props)
    assert "sk-test" not in str(props)
