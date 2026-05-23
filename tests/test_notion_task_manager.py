import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from notion_task_manager import (
    NotionTaskConfig,
    ensure_task_database_schema,
    load_notion_task_config,
    notion_task_health,
    task_database_properties,
    upsert_task,
)


class FakeNotion:
    def __init__(self):
        self.databases = {}
        self.pages = []
        self.updated_pages = []

    def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {"Title": {"title": {}}}}

    def create_database(self, *, parent_page_id, title, properties):
        self.databases["created"] = {
            "parent_page_id": parent_page_id,
            "title": title,
            "properties": properties,
        }
        return {"id": "db-created"}

    def update_database(self, database_id, *, properties):
        self.databases["updated"] = {"database_id": database_id, "properties": properties}
        return {"id": database_id}

    def query_database(self, database_id, payload=None):
        payload = payload or {}
        query_filter = payload.get("filter") or {}
        prop = query_filter.get("property")
        rich_text = query_filter.get("rich_text") or {}
        expected = rich_text.get("equals")
        if prop and expected is not None:
            matches = [
                page
                for page in self.pages
                if _plain_rich_text(page.get("properties", {}).get(prop)) == expected
            ]
            return {"results": matches[: payload.get("page_size", 100)]}
        return {"results": self.pages[: payload.get("page_size", 100)]}

    def create_page(self, *, database_id, properties):
        page = {"id": "page-created", "properties": properties}
        self.pages.append(page)
        return page

    def update_page(self, page_id, *, properties):
        self.updated_pages.append({"id": page_id, "properties": properties})
        return {"id": page_id, "properties": properties}


def test_load_config_uses_existing_openclaw_notion_env_without_exposing_token(tmp_path, monkeypatch):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "skills": {
                    "entries": {
                        "notion-direct": {"env": {"NOTION_API_KEY": "secret-token"}},
                        "student-research": {"env": {"NOTION_PARENT_PAGE_ID": "parent-id"}},
                    }
                }
            }
        )
    )

    config = load_notion_task_config(openclaw_config_path=config_path, state_file=tmp_path / "state.json")
    health = notion_task_health(config)

    assert config.token == "secret-token"
    assert health["token_configured"] is True
    assert "secret-token" not in json.dumps(health)


def test_schema_ensure_dry_run_has_no_writes(tmp_path):
    config = NotionTaskConfig(
        token="secret-token",
        database_id=None,
        parent_page_id="parent-id",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )

    payload = ensure_task_database_schema(live=False, config=config, client=FakeNotion())

    assert payload["status"] == "dry_run"
    assert payload["would_create_database"] is True
    assert payload["notion_write_attempted"] is False


def test_schema_ensure_live_creates_dedicated_database_and_state(tmp_path):
    fake = FakeNotion()
    config = NotionTaskConfig(
        token="secret-token",
        database_id=None,
        parent_page_id="parent-id",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )

    payload = ensure_task_database_schema(live=True, config=config, client=fake)

    assert payload["status"] == "created"
    assert fake.databases["created"]["properties"].keys() >= task_database_properties().keys()
    assert json.loads((tmp_path / "state.json").read_text())["notion_task_database_id"] == "db-created"


def test_upsert_task_creates_and_updates_by_dedupe_key(tmp_path):
    fake = FakeNotion()
    config = NotionTaskConfig(
        token="secret-token",
        database_id="db-id",
        parent_page_id="parent-id",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )
    task = {
        "title": "Review investor follow-up",
        "description": "Dusan should review the investor follow-up.",
        "priority": "High",
        "source": "Email",
        "source_ref": "apple-mail:message:abc",
        "confidence": 0.9,
        "dedupe_key": "task:abc",
    }

    first = upsert_task(task, live=True, config=config, client=fake)
    second = upsert_task(task, live=True, config=config, client=fake)

    assert first["status"] == "created"
    assert second["status"] == "updated"
    assert fake.updated_pages
    assert "investor" in json.dumps(first)


def test_stable_dedupe_prefers_source_ref(tmp_path):
    from notion_task_manager import stable_task_dedupe_key

    first = stable_task_dedupe_key({"title": "A changing summary", "source_ref": "apple-mail:message:abc"})
    second = stable_task_dedupe_key({"title": "A different generated title", "source_ref": "apple-mail:message:abc"})

    assert first == second
    assert first.startswith("task-source:")


def test_upsert_task_falls_back_to_source_ref_when_dedupe_changed(tmp_path):
    fake = FakeNotion()
    config = NotionTaskConfig(
        token="secret-token",
        database_id="db-id",
        parent_page_id="parent-id",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )
    fake.pages.append(
        {
            "id": "legacy-page",
            "properties": {
                "Title": {"title": [{"type": "text", "text": {"content": "Old generated wording"}}]},
                "Dedupe key": {"rich_text": [{"type": "text", "text": {"content": "task:old-title-key"}}]},
                "Source ref": {"rich_text": [{"type": "text", "text": {"content": "apple-mail:message:abc"}}]},
            },
        }
    )

    payload = upsert_task(
        {
            "title": "New generated wording for the same message",
            "source": "Email",
            "source_ref": "apple-mail:message:abc",
            "confidence": 0.88,
        },
        live=True,
        config=config,
        client=fake,
    )

    assert payload["status"] == "updated"
    assert fake.updated_pages[0]["id"] == "legacy-page"
    updated_json = json.dumps(fake.updated_pages[0])
    assert "task-source:" in updated_json
    assert "task:old-title-key" not in updated_json


def test_schema_ensure_live_returns_blocked_on_notion_error(tmp_path):
    class FailingNotion(FakeNotion):
        def create_database(self, *, parent_page_id, title, properties):
            raise RuntimeError("notion_http_404:hash")

    config = NotionTaskConfig(
        token="secret-token",
        database_id=None,
        parent_page_id="parent-id",
        state_file=tmp_path / "state.json",
        openclaw_config_path=tmp_path / "openclaw.json",
    )

    payload = ensure_task_database_schema(live=True, config=config, client=FailingNotion())

    assert payload["status"] == "blocked"
    assert payload["reason"] == "notion_schema_ensure_failed"
    assert "notion_http_404" not in json.dumps(payload)


def _plain_rich_text(prop):
    return "".join((item.get("plain_text") or item.get("text", {}).get("content") or "") for item in (prop or {}).get("rich_text", []))
