#!/usr/bin/env python3
"""Notion-backed task database manager for Rocky's personal task spine."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENCLAW_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/openclaw.json")
DEFAULT_TOKEN_FILE = Path("/Users/clawdbot/.config/notion/api_key_rockaway_full")
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/rocky_task_spine.json")
DEFAULT_DATABASE_TITLE = "Rocky Personal Task Spine"
NOTION_VERSION = "2022-06-28"
POLICY_VERSION = "rocky-task-spine-v1"
TASK_STATUSES = {"Candidate", "Open", "Scheduled", "Waiting", "Done", "Cancelled", "Archived"}
TERMINAL_TASK_STATUSES = {"Done", "Cancelled", "Archived"}
TASK_PRIORITIES = {"Low", "Normal", "High", "Urgent"}
SENSITIVE_KEY_RE = re.compile(
    r"(auth|body|content|cookie|credential|html|password|raw|secret|token|transcript)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"(?:cookie|token|secret|password|credential)\s*[:=]|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NotionTaskConfig:
    token: str | None
    database_id: str | None
    parent_page_id: str | None
    state_file: Path
    openclaw_config_path: Path

    @property
    def token_configured(self) -> bool:
        return bool(self.token)

    @property
    def database_configured(self) -> bool:
        return bool(self.database_id)

    @property
    def parent_configured(self) -> bool:
        return bool(self.parent_page_id)


class NotionClient:
    """Small Notion REST wrapper; tests pass a fake client at this boundary."""

    def __init__(self, token: str, *, api_base: str = "https://api.notion.com/v1"):
        self.token = token
        self.api_base = api_base.rstrip("/")

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def create_database(self, *, parent_page_id: str, title: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/databases",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties,
            },
        )

    def update_database(self, database_id: str, *, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/databases/{database_id}", {"properties": properties})

    def query_database(self, database_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", f"/databases/{database_id}/query", payload or {})

    def create_page(self, *, database_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/pages",
            {"parent": {"database_id": database_id}, "properties": properties},
        )

    def update_page(self, page_id: str, *, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.api_base}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
            method=method,
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"notion_http_{exc.code}:{_hash_text(body)}") from exc


def load_notion_task_config(
    *,
    openclaw_config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    state_file: str | Path = DEFAULT_STATE_FILE,
    token_file: str | Path = DEFAULT_TOKEN_FILE,
) -> NotionTaskConfig:
    config_path = Path(openclaw_config_path).expanduser()
    state_path = Path(state_file).expanduser()
    openclaw = _read_json(config_path)
    state = _read_json(state_path)
    skills = ((openclaw.get("skills") or {}).get("entries") or {})
    notion_direct_env = ((skills.get("notion-direct") or {}).get("env") or {})
    student_env = ((skills.get("student-research") or {}).get("env") or {})
    database_id = (
        os.getenv("ROCKY_TASK_DATABASE_ID")
        or state.get("notion_task_database_id")
        or notion_direct_env.get("ROCKY_TASK_DATABASE_ID")
        or student_env.get("ROCKY_TASK_DATABASE_ID")
    )
    parent_page_id = (
        os.getenv("ROCKY_TASK_PARENT_PAGE_ID")
        or state.get("notion_task_parent_page_id")
        or notion_direct_env.get("ROCKY_TASK_PARENT_PAGE_ID")
        or student_env.get("ROCKY_TASK_PARENT_PAGE_ID")
        or student_env.get("NOTION_PARENT_PAGE_ID")
    )
    token = (
        os.getenv("ROCKY_TASK_NOTION_API_KEY")
        or os.getenv("NOTION_API_KEY")
        or (
            student_env.get("NOTION_INTEGRATION_TOKEN")
            if parent_page_id and str(parent_page_id) == str(student_env.get("NOTION_PARENT_PAGE_ID") or "")
            else None
        )
        or notion_direct_env.get("NOTION_API_KEY")
        or student_env.get("NOTION_INTEGRATION_TOKEN")
        or _read_secret_file(Path(token_file).expanduser())
    )
    return NotionTaskConfig(
        token=str(token).strip() if token else None,
        database_id=_normalize_notion_id(database_id),
        parent_page_id=_normalize_notion_id(parent_page_id),
        state_file=state_path,
        openclaw_config_path=config_path,
    )


def notion_task_health(config: NotionTaskConfig | None = None) -> dict[str, Any]:
    config = config or load_notion_task_config()
    status = "ok" if config.token_configured and (config.database_configured or config.parent_configured) else "blocked"
    reason = None
    if not config.token_configured:
        reason = "notion_token_missing"
    elif not config.database_configured and not config.parent_configured:
        reason = "notion_database_or_parent_missing"
    return {
        "status": status,
        "reason": reason,
        "token_configured": config.token_configured,
        "database_configured": config.database_configured,
        "parent_configured": config.parent_configured,
        "database_id_hash": _hash_text(config.database_id or ""),
        "parent_page_id_hash": _hash_text(config.parent_page_id or ""),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def task_database_properties() -> dict[str, Any]:
    return {
        "Title": {"title": {}},
        "Description": {"rich_text": {}},
        "Status": {"select": {"options": [{"name": item} for item in sorted(TASK_STATUSES)]}},
        "Priority": {"select": {"options": [{"name": item} for item in ["Low", "Normal", "High", "Urgent"]]}},
        "Owner": {"rich_text": {}},
        "Requires Dusan Action": {"checkbox": {}},
        "Requires Rocky Action": {"checkbox": {}},
        "Due date": {"date": {}},
        "Created date": {"date": {}},
        "Updated date": {"date": {}},
        "Source": {"select": {"options": [{"name": item} for item in ["Email", "Meeting", "Memory", "Discord", "Command", "Calendar", "Code"]]}},
        "Source ref": {"rich_text": {}},
        "Confidence": {"number": {"format": "percent"}},
        "Evidence hash": {"rich_text": {}},
        "Estimated effort minutes": {"number": {"format": "number"}},
        "Next reminder date": {"date": {}},
        "Last reminded date": {"date": {}},
        "Reminder count": {"number": {"format": "number"}},
        "Calendar block status": {"select": {"options": [{"name": item} for item in ["None", "Proposed", "Scheduled", "Skipped", "Blocked"]]}},
        "Calendar idempotency key": {"rich_text": {}},
        "Related project": {"rich_text": {}},
        "Related person/company": {"rich_text": {}},
        "Completion signal": {"rich_text": {}},
        "Cancelled/archived reason": {"rich_text": {}},
        "Dedupe key": {"rich_text": {}},
        "Action fingerprint": {"rich_text": {}},
        "Last detected date": {"date": {}},
        "Detection count": {"number": {"format": "number"}},
        "Last lifecycle reason": {"rich_text": {}},
        "Rocky task id": {"rich_text": {}},
    }


def ensure_task_database_schema(
    *,
    live: bool = False,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
    database_title: str = DEFAULT_DATABASE_TITLE,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    health = notion_task_health(config)
    if health["status"] != "ok":
        return {**health, "side_effects": [], "database_id": None}
    expected = task_database_properties()
    if not live:
        return {
            "status": "dry_run",
            "reason": "live_flag_not_supplied",
            "database_id": config.database_id,
            "would_create_database": not config.database_configured,
            "expected_property_count": len(expected),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
            "side_effects": [],
        }
    notion = client or NotionClient(config.token or "")
    try:
        if config.database_configured:
            existing = notion.retrieve_database(config.database_id or "")
            existing_props = existing.get("properties") or {}
            missing = {name: schema for name, schema in expected.items() if name not in existing_props}
            if missing:
                notion.update_database(config.database_id or "", properties=missing)
            _write_state(config.state_file, {"notion_task_database_id": config.database_id, "updated_at": _utc_now()})
            return {
                "status": "ok",
                "reason": "database_verified",
                "database_id": config.database_id,
                "missing_property_count": len(missing),
                "calendar_write_attempted": False,
                "notion_write_attempted": bool(missing),
                "side_effects": ["notion_database_updated"] if missing else [],
            }
        created = notion.create_database(
            parent_page_id=config.parent_page_id or "",
            title=database_title,
            properties=expected,
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": "notion_schema_ensure_failed",
            "failure_class": exc.__class__.__name__,
            "error_hash": _hash_text(str(exc)),
            "database_id": config.database_id,
            "calendar_write_attempted": False,
            "notion_write_attempted": True,
            "side_effects": [],
        }
    database_id = _normalize_notion_id(created.get("id"))
    _write_state(config.state_file, {"notion_task_database_id": database_id, "notion_task_parent_page_id": config.parent_page_id, "updated_at": _utc_now()})
    return {
        "status": "created",
        "reason": "database_created",
        "database_id": database_id,
        "calendar_write_attempted": False,
        "notion_write_attempted": True,
        "side_effects": ["notion_database_created", "local_state_file"],
    }


def upsert_task(
    task: dict[str, Any],
    *,
    live: bool = False,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    if not live:
        return {
            "status": "dry_run",
            "reason": "live_flag_not_supplied",
            "task": _safe_task_summary(task),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    if not config.token_configured or not config.database_configured:
        return {
            "status": "blocked",
            "reason": "notion_task_database_not_configured",
            "task": _safe_task_summary(task),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    notion = client or NotionClient(config.token or "")
    action_fingerprint = str(task.get("action_fingerprint") or stable_task_action_fingerprint(task))
    dedupe_key = str(task.get("dedupe_key") or stable_task_dedupe_key({**task, "action_fingerprint": action_fingerprint}))
    explicit_page_id = str(task.get("existing_page_id") or "").strip()
    existing = {"id": explicit_page_id} if explicit_page_id else _find_page_by_task_identity(
        notion,
        config.database_id or "",
        dedupe_key=dedupe_key,
        action_fingerprint=action_fingerprint,
    )
    properties = task_to_notion_properties({**task, "dedupe_key": dedupe_key, "action_fingerprint": action_fingerprint})
    if existing:
        updated = notion.update_page(existing["id"], properties=properties)
        return {
            "status": "updated",
            "reason": "existing_task_updated",
            "page_id": updated.get("id") or existing.get("id"),
            "dedupe_key": dedupe_key,
            "task": _safe_task_summary(task),
            "calendar_write_attempted": False,
            "notion_write_attempted": True,
        }
    created = notion.create_page(database_id=config.database_id or "", properties=properties)
    return {
        "status": "created",
        "reason": "task_created",
        "page_id": created.get("id"),
        "dedupe_key": dedupe_key,
        "task": _safe_task_summary(task),
        "calendar_write_attempted": False,
        "notion_write_attempted": True,
    }


def list_tasks(
    *,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    if not config.token_configured or not config.database_configured:
        return {"status": "blocked", "reason": "notion_task_database_not_configured", "tasks": []}
    notion = client or NotionClient(config.token or "")
    payload = notion.query_database(config.database_id or "", {"page_size": max(1, min(int(limit), 100))})
    return {
        "status": "ok",
        "tasks": [notion_page_to_task(item) for item in payload.get("results") or []],
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def list_open_tasks(
    *,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    if not config.token_configured or not config.database_configured:
        return {"status": "blocked", "reason": "notion_task_database_not_configured", "tasks": []}
    notion = client or NotionClient(config.token or "")
    payload = notion.query_database(
        config.database_id or "",
        {
            "page_size": max(1, min(int(limit), 100)),
            "filter": {
                "or": [
                    {"property": "Status", "select": {"equals": "Open"}},
                    {"property": "Status", "select": {"equals": "Scheduled"}},
                    {"property": "Status", "select": {"equals": "Candidate"}},
                    {"property": "Status", "select": {"equals": "Waiting"}},
                ]
            },
        },
    )
    return {
        "status": "ok",
        "tasks": [notion_page_to_task(item) for item in payload.get("results") or []],
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def mark_task_calendar_status(
    *,
    page_id: str,
    calendar_status: str,
    idempotency_key: str | None = None,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    if not config.token_configured:
        return {"status": "blocked", "reason": "notion_token_missing"}
    notion = client or NotionClient(config.token or "")
    props = {
        "Calendar block status": _select_prop(calendar_status),
        "Updated date": _date_prop(date.today().isoformat()),
    }
    if idempotency_key:
        props["Calendar idempotency key"] = _rich_text_prop(idempotency_key)
    updated = notion.update_page(page_id, properties=props)
    return {"status": "updated", "page_id": updated.get("id") or page_id, "notion_write_attempted": True}


def update_task_reminder_metadata(
    *,
    page_id: str,
    last_reminded_date: str,
    next_reminder_date: str,
    reminder_count: int,
    lifecycle_reason: str = "reminder_sent",
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_notion_task_config()
    if not config.token_configured:
        return {"status": "blocked", "reason": "notion_token_missing", "notion_write_attempted": False}
    notion = client or NotionClient(config.token or "")
    props = {
        "Last reminded date": _date_prop(last_reminded_date),
        "Next reminder date": _date_prop(next_reminder_date),
        "Reminder count": {"number": int(reminder_count)},
        "Last lifecycle reason": _rich_text_prop(lifecycle_reason),
        "Updated date": _date_prop(date.today().isoformat()),
    }
    updated = notion.update_page(page_id, properties=props)
    return {
        "status": "updated",
        "reason": lifecycle_reason,
        "page_id": updated.get("id") or page_id,
        "notion_write_attempted": True,
    }


def update_task_status(
    *,
    page_id: str,
    status: str,
    lifecycle_reason: str,
    completion_signal: str | None = None,
    cancelled_archived_reason: str | None = None,
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Update terminal/open task status through Notion only."""
    config = config or load_notion_task_config()
    if not config.token_configured:
        return {"status": "blocked", "reason": "notion_token_missing", "notion_write_attempted": False}
    if not page_id:
        return {"status": "blocked", "reason": "notion_page_id_missing", "notion_write_attempted": False}
    notion = client or NotionClient(config.token or "")
    props = {
        "Status": _select_prop(_safe_status(status)),
        "Last lifecycle reason": _rich_text_prop(lifecycle_reason),
        "Updated date": _date_prop(date.today().isoformat()),
    }
    if completion_signal:
        props["Completion signal"] = _rich_text_prop(completion_signal)
    if cancelled_archived_reason:
        props["Cancelled/archived reason"] = _rich_text_prop(cancelled_archived_reason)
    updated = notion.update_page(page_id, properties=props)
    return {
        "status": "updated",
        "reason": lifecycle_reason,
        "page_id": updated.get("id") or page_id,
        "task_status": _safe_status(status),
        "calendar_write_attempted": False,
        "notion_write_attempted": True,
    }


def update_task_due_date(
    *,
    page_id: str,
    due_date: str,
    lifecycle_reason: str = "due_date_updated",
    config: NotionTaskConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Update a task due date without touching Calendar."""
    config = config or load_notion_task_config()
    if not config.token_configured:
        return {"status": "blocked", "reason": "notion_token_missing", "notion_write_attempted": False}
    if not page_id:
        return {"status": "blocked", "reason": "notion_page_id_missing", "notion_write_attempted": False}
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(due_date or "")):
        return {"status": "blocked", "reason": "invalid_due_date", "notion_write_attempted": False}
    notion = client or NotionClient(config.token or "")
    props = {
        "Due date": _date_prop(due_date),
        "Last lifecycle reason": _rich_text_prop(lifecycle_reason),
        "Updated date": _date_prop(date.today().isoformat()),
    }
    updated = notion.update_page(page_id, properties=props)
    return {
        "status": "updated",
        "reason": lifecycle_reason,
        "page_id": updated.get("id") or page_id,
        "due_date": due_date,
        "calendar_write_attempted": False,
        "notion_write_attempted": True,
    }


def task_to_notion_properties(task: dict[str, Any]) -> dict[str, Any]:
    due_date = task.get("due_date")
    today = date.today().isoformat()
    return {
        "Title": {"title": [{"type": "text", "text": {"content": _safe_text(task.get("title"), 180)}}]},
        "Description": _rich_text_prop(_safe_text(task.get("description"), 1200)),
        "Status": _select_prop(_safe_status(task.get("status"))),
        "Priority": _select_prop(_safe_priority(task.get("priority"))),
        "Owner": _rich_text_prop(_safe_text(task.get("owner") or "Dusan", 120)),
        "Requires Dusan Action": {"checkbox": bool(task.get("requires_dusan_action", True))},
        "Requires Rocky Action": {"checkbox": bool(task.get("requires_rocky_action", False))},
        "Due date": _date_prop(due_date),
        "Created date": _date_prop(task.get("created_date") or today),
        "Updated date": _date_prop(today),
        "Source": _select_prop(_safe_source(task.get("source"))),
        "Source ref": _rich_text_prop(_safe_text(task.get("source_ref"), 500)),
        "Confidence": {"number": float(task.get("confidence") or 0)},
        "Evidence hash": _rich_text_prop(_safe_text(task.get("evidence_hash"), 120)),
        "Estimated effort minutes": {"number": int(task.get("estimated_effort_minutes") or 30)},
        "Next reminder date": _date_prop(task.get("next_reminder_date")),
        "Last reminded date": _date_prop(task.get("last_reminded_date")),
        "Reminder count": {"number": int(task.get("reminder_count") or 0)},
        "Calendar block status": _select_prop(_safe_calendar_status(task.get("calendar_block_status"))),
        "Calendar idempotency key": _rich_text_prop(_safe_text(task.get("calendar_idempotency_key"), 240)),
        "Action fingerprint": _rich_text_prop(_safe_text(task.get("action_fingerprint") or stable_task_action_fingerprint(task), 160)),
        "Last detected date": _date_prop(task.get("last_detected_date") or today),
        "Detection count": {"number": int(task.get("detection_count") or 1)},
        "Last lifecycle reason": _rich_text_prop(_safe_text(task.get("last_lifecycle_reason"), 240)),
        "Related project": _rich_text_prop(_safe_text(task.get("related_project"), 240)),
        "Related person/company": _rich_text_prop(_safe_text(task.get("related_person_company"), 240)),
        "Completion signal": _rich_text_prop(_safe_text(task.get("completion_signal"), 500)),
        "Cancelled/archived reason": _rich_text_prop(_safe_text(task.get("cancelled_archived_reason"), 500)),
        "Dedupe key": _rich_text_prop(_safe_text(task.get("dedupe_key"), 240)),
        "Rocky task id": _rich_text_prop(_safe_text(task.get("rocky_task_id") or stable_task_id(task), 120)),
    }


def notion_page_to_task(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("properties") or {}
    return {
        "page_id": page.get("id"),
        "title": _plain_title(props.get("Title")),
        "description": _plain_rich_text(props.get("Description")),
        "status": _plain_select(props.get("Status")) or "Open",
        "priority": _plain_select(props.get("Priority")) or "Normal",
        "owner": _plain_rich_text(props.get("Owner")) or "Dusan",
        "requires_dusan_action": _plain_checkbox(props.get("Requires Dusan Action")),
        "requires_rocky_action": _plain_checkbox(props.get("Requires Rocky Action")),
        "due_date": _plain_date(props.get("Due date")),
        "source": _plain_select(props.get("Source")),
        "source_ref": _plain_rich_text(props.get("Source ref")),
        "confidence": _plain_number(props.get("Confidence")),
        "evidence_hash": _plain_rich_text(props.get("Evidence hash")),
        "estimated_effort_minutes": int(_plain_number(props.get("Estimated effort minutes")) or 30),
        "next_reminder_date": _plain_date(props.get("Next reminder date")),
        "last_reminded_date": _plain_date(props.get("Last reminded date")),
        "reminder_count": int(_plain_number(props.get("Reminder count")) or 0),
        "calendar_block_status": _plain_select(props.get("Calendar block status")) or "None",
        "calendar_idempotency_key": _plain_rich_text(props.get("Calendar idempotency key")),
        "action_fingerprint": _plain_rich_text(props.get("Action fingerprint")),
        "last_detected_date": _plain_date(props.get("Last detected date")),
        "detection_count": int(_plain_number(props.get("Detection count")) or 0),
        "last_lifecycle_reason": _plain_rich_text(props.get("Last lifecycle reason")),
        "related_project": _plain_rich_text(props.get("Related project")),
        "related_person_company": _plain_rich_text(props.get("Related person/company")),
        "dedupe_key": _plain_rich_text(props.get("Dedupe key")),
        "rocky_task_id": _plain_rich_text(props.get("Rocky task id")),
    }


def stable_task_action_fingerprint(task: dict[str, Any]) -> str:
    payload = {
        "title": _normalize_text(task.get("title")),
        "owner": _normalize_text(task.get("owner") or "Dusan"),
        "source": _normalize_text(task.get("source")),
        "related_project": _normalize_text(task.get("related_project")),
        "related_person_company": _normalize_text(task.get("related_person_company")),
        "due_date": str(task.get("due_date") or ""),
    }
    return f"task-action:{_hash_json(payload)}"


def legacy_source_dedupe_key(task: dict[str, Any]) -> str:
    source_ref = str(task.get("source_ref") or "").strip()
    if not source_ref:
        return ""
    return f"task-source:{_hash_json({'source_ref': source_ref, 'owner': _normalize_text(task.get('owner') or 'Dusan')})}"


def stable_task_dedupe_key(task: dict[str, Any]) -> str:
    action_fingerprint = str(task.get("action_fingerprint") or stable_task_action_fingerprint(task))
    source_ref = str(task.get("source_ref") or "").strip()
    if source_ref:
        return f"task-source-action:{_hash_json({'source_ref': source_ref, 'action_fingerprint': action_fingerprint, 'owner': _normalize_text(task.get('owner') or 'Dusan')})}"
    return f"task-action-key:{_hash_json({'action_fingerprint': action_fingerprint, 'owner': _normalize_text(task.get('owner') or 'Dusan')})}"


def stable_task_id(task: dict[str, Any]) -> str:
    return f"rocky-task:{_hash_json({'dedupe_key': task.get('dedupe_key') or stable_task_dedupe_key(task), 'action_fingerprint': task.get('action_fingerprint') or stable_task_action_fingerprint(task)})}"


def _find_page_by_task_identity(
    notion: Any,
    database_id: str,
    *,
    dedupe_key: str,
    action_fingerprint: str = "",
) -> dict[str, Any] | None:
    payload = notion.query_database(
        database_id,
        {"filter": {"property": "Dedupe key", "rich_text": {"equals": dedupe_key}}, "page_size": 1},
    )
    results = payload.get("results") or []
    if results:
        return results[0]
    if action_fingerprint:
        payload = notion.query_database(
            database_id,
            {"filter": {"property": "Action fingerprint", "rich_text": {"equals": action_fingerprint}}, "page_size": 1},
        )
        results = payload.get("results") or []
        if results:
            return results[0]
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, updates: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_json(path)
    payload.update(updates)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def _normalize_notion_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _select_prop(name: str | None) -> dict[str, Any]:
    return {"select": {"name": str(name or "Normal")}}


def _rich_text_prop(value: Any) -> dict[str, Any]:
    text = _safe_text(value, 1800)
    return {"rich_text": ([{"type": "text", "text": {"content": text}}] if text else [])}


def _date_prop(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    return {"date": {"start": text}} if text else {"date": None}


def _safe_status(value: Any) -> str:
    text = str(value or "Open").strip().title()
    return text if text in TASK_STATUSES else "Open"


def _safe_priority(value: Any) -> str:
    text = str(value or "Normal").strip().title()
    return text if text in TASK_PRIORITIES else "Normal"


def _safe_source(value: Any) -> str:
    text = str(value or "Memory").strip().title()
    return text if text in {"Email", "Meeting", "Memory", "Discord", "Command", "Calendar", "Code"} else "Memory"


def _safe_calendar_status(value: Any) -> str:
    text = str(value or "None").strip().title()
    return text if text in {"None", "Proposed", "Scheduled", "Skipped", "Blocked"} else "None"


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{_hash_text(text)}]"
    return text[:limit]


def _safe_task_summary(task: dict[str, Any]) -> dict[str, Any]:
    return _redact(
        {
            "title": task.get("title"),
            "status": task.get("status"),
            "priority": task.get("priority"),
            "owner": task.get("owner"),
            "source": task.get("source"),
            "source_ref": task.get("source_ref"),
            "confidence": task.get("confidence"),
            "dedupe_key": task.get("dedupe_key"),
            "action_fingerprint": task.get("action_fingerprint"),
            "rocky_task_id": task.get("rocky_task_id"),
            "estimated_effort_minutes": task.get("estimated_effort_minutes"),
        }
    )


def _redact(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and SENSITIVE_KEY_RE.search(parent_key):
        return {"redacted": True, "sha256": _hash_text(json.dumps(value, sort_keys=True, default=str))}
    if isinstance(value, dict):
        return {str(k): _redact(v, parent_key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": _hash_text(value), "chars": len(value)}
    return value


def _plain_title(prop: dict[str, Any] | None) -> str:
    return "".join((item.get("plain_text") or item.get("text", {}).get("content") or "") for item in ((prop or {}).get("title") or []))


def _plain_rich_text(prop: dict[str, Any] | None) -> str:
    return "".join((item.get("plain_text") or item.get("text", {}).get("content") or "") for item in ((prop or {}).get("rich_text") or []))


def _plain_select(prop: dict[str, Any] | None) -> str | None:
    selected = (prop or {}).get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _plain_checkbox(prop: dict[str, Any] | None) -> bool:
    return bool((prop or {}).get("checkbox"))


def _plain_number(prop: dict[str, Any] | None) -> float | None:
    value = (prop or {}).get("number")
    return float(value) if isinstance(value, (int, float)) else None


def _plain_date(prop: dict[str, Any] | None) -> str | None:
    value = (prop or {}).get("date")
    return value.get("start") if isinstance(value, dict) else None


def _normalize_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16] if value else ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
