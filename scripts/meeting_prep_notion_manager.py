#!/usr/bin/env python3
"""Notion database manager for Rocky meeting prep notes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from notion_task_manager import (
    DEFAULT_OPENCLAW_CONFIG_PATH,
    DEFAULT_TOKEN_FILE,
    NOTION_VERSION,
    NotionClient,
    load_notion_task_config,
    _normalize_notion_id,
    _read_json,
    _read_secret_file,
)

DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/rocky_meeting_prep.json")
DEFAULT_DATABASE_TITLE = "Rocky Meeting Prep"
SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NotionMeetingPrepConfig:
    token: str | None
    database_id: str | None
    parent_page_id: str | None
    state_file: Path

    @property
    def token_configured(self) -> bool:
        return bool(self.token)

    @property
    def database_configured(self) -> bool:
        return bool(self.database_id)

    @property
    def parent_configured(self) -> bool:
        return bool(self.parent_page_id)


def load_meeting_prep_config(
    *,
    openclaw_config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    state_file: str | Path = DEFAULT_STATE_FILE,
    token_file: str | Path = DEFAULT_TOKEN_FILE,
) -> NotionMeetingPrepConfig:
    config_path = Path(openclaw_config_path).expanduser()
    state_path = Path(state_file).expanduser()
    task_config = load_notion_task_config(openclaw_config_path=openclaw_config_path)
    openclaw = _read_json(config_path)
    state = _read_json(state_path)
    skills = ((openclaw.get("skills") or {}).get("entries") or {})
    notion_direct_env = ((skills.get("notion-direct") or {}).get("env") or {})
    student_env = ((skills.get("student-research") or {}).get("env") or {})
    database_id = state.get("notion_meeting_prep_database_id") or notion_direct_env.get("ROCKY_MEETING_PREP_DATABASE_ID")
    parent_page_id = (
        state.get("notion_meeting_prep_parent_page_id")
        or notion_direct_env.get("ROCKY_MEETING_PREP_PARENT_PAGE_ID")
        or notion_direct_env.get("ROCKY_TASK_PARENT_PAGE_ID")
        or task_config.parent_page_id
        or student_env.get("NOTION_PARENT_PAGE_ID")
    )
    token = task_config.token or notion_direct_env.get("NOTION_API_KEY") or student_env.get("NOTION_INTEGRATION_TOKEN") or _read_secret_file(Path(token_file).expanduser())
    return NotionMeetingPrepConfig(
        token=str(token).strip() if token else None,
        database_id=_normalize_notion_id(database_id),
        parent_page_id=_normalize_notion_id(parent_page_id),
        state_file=state_path,
    )


def meeting_prep_notion_health(config: NotionMeetingPrepConfig | None = None) -> dict[str, Any]:
    config = config or load_meeting_prep_config()
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


def meeting_prep_database_properties() -> dict[str, Any]:
    return {
        "Title": {"title": {}},
        "Meeting date": {"date": {}},
        "Meeting key": {"rich_text": {}},
        "Calendar event ref": {"rich_text": {}},
        "Status": {"select": {"options": [{"name": item} for item in ["Candidate", "Ready", "Sent", "Skipped", "Archived"]]}},
        "Context confidence": {"number": {"format": "percent"}},
        "People / companies": {"rich_text": {}},
        "Source refs": {"rich_text": {}},
        "Dusan notes": {"rich_text": {}},
        "Brief": {"rich_text": {}},
        "Questions": {"rich_text": {}},
        "Open loops": {"rich_text": {}},
        "Discord status": {"select": {"options": [{"name": item} for item in ["Not sent", "Dry run", "Sent", "Failed"]]}},
        "Outcome status": {"select": {"options": [{"name": item} for item in ["Not captured", "Captured", "Applied", "Manual review", "Skipped"]]}},
        "Outcome summary": {"rich_text": {}},
        "Decisions": {"rich_text": {}},
        "Follow-up task refs": {"rich_text": {}},
        "Memory refs": {"rich_text": {}},
        "Outcome hash": {"rich_text": {}},
        "Outcome captured at": {"date": {}},
        "Last sent at": {"date": {}},
        "Message hash": {"rich_text": {}},
        "Updated date": {"date": {}},
    }


def ensure_meeting_prep_database_schema(
    *,
    live: bool = False,
    config: NotionMeetingPrepConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    config = config or load_meeting_prep_config()
    health = meeting_prep_notion_health(config)
    expected = meeting_prep_database_properties()
    if health["status"] != "ok":
        return {**health, "side_effects": []}
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
            _write_state(config.state_file, {"notion_meeting_prep_database_id": config.database_id, "notion_meeting_prep_parent_page_id": config.parent_page_id})
            return {
                "status": "ok",
                "reason": "database_verified",
                "database_id": config.database_id,
                "missing_property_count": len(missing),
                "calendar_write_attempted": False,
                "notion_write_attempted": bool(missing),
                "side_effects": ["notion_database_updated"] if missing else [],
            }
        created = notion.create_database(parent_page_id=config.parent_page_id or "", title=DEFAULT_DATABASE_TITLE, properties=expected)
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": "notion_meeting_prep_schema_ensure_failed",
            "failure_class": exc.__class__.__name__,
            "error_hash": _hash_text(str(exc)),
            "database_id": config.database_id,
            "calendar_write_attempted": False,
            "notion_write_attempted": True,
            "side_effects": [],
        }
    database_id = _normalize_notion_id(created.get("id"))
    _write_state(config.state_file, {"notion_meeting_prep_database_id": database_id, "notion_meeting_prep_parent_page_id": config.parent_page_id})
    return {
        "status": "created",
        "reason": "database_created",
        "database_id": database_id,
        "calendar_write_attempted": False,
        "notion_write_attempted": True,
        "side_effects": ["notion_database_created", "local_state_file"],
    }


def upsert_meeting_prep_note(
    meeting: dict[str, Any],
    brief_payload: dict[str, Any],
    *,
    live: bool = False,
    config: NotionMeetingPrepConfig | None = None,
    client: Any | None = None,
    discord_status: str = "Not sent",
) -> dict[str, Any]:
    config = config or load_meeting_prep_config()
    summary = _safe_summary(meeting, brief_payload)
    if not live:
        return {"status": "dry_run", "reason": "live_flag_not_supplied", "meeting_prep": summary, "calendar_write_attempted": False, "notion_write_attempted": False}
    if not config.token_configured or not config.database_configured:
        return {"status": "blocked", "reason": "notion_meeting_prep_database_not_configured", "meeting_prep": summary, "calendar_write_attempted": False, "notion_write_attempted": False}
    notion = client or NotionClient(config.token or "")
    meeting_key = str(meeting.get("meeting_key") or brief_payload.get("meeting_key") or "")
    existing = _find_page_by_meeting_key(notion, config.database_id or "", meeting_key)
    props = meeting_prep_to_properties(meeting, brief_payload, discord_status=discord_status)
    if existing:
        updated = notion.update_page(existing["id"], properties=props)
        return {"status": "updated", "reason": "existing_meeting_prep_updated", "page_id": updated.get("id") or existing.get("id"), "meeting_key": meeting_key, "meeting_prep": summary, "calendar_write_attempted": False, "notion_write_attempted": True}
    created = notion.create_page(database_id=config.database_id or "", properties=props)
    return {"status": "created", "reason": "meeting_prep_created", "page_id": created.get("id"), "meeting_key": meeting_key, "meeting_prep": summary, "calendar_write_attempted": False, "notion_write_attempted": True}


def update_meeting_prep_outcome(
    outcome: dict[str, Any],
    *,
    task_result: dict[str, Any] | None = None,
    memory_result: dict[str, Any] | None = None,
    live: bool = False,
    config: NotionMeetingPrepConfig | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Update an existing meeting prep page with post-meeting outcome metadata."""
    config = config or load_meeting_prep_config()
    summary = _safe_outcome_summary(outcome, task_result or {}, memory_result or {})
    if not live:
        return {"status": "dry_run", "reason": "live_flag_not_supplied", "meeting_key": outcome.get("meeting_key"), "outcome": summary, "calendar_write_attempted": False, "notion_write_attempted": False}
    if not config.token_configured or not config.database_configured:
        return {"status": "blocked", "reason": "notion_meeting_prep_database_not_configured", "meeting_key": outcome.get("meeting_key"), "calendar_write_attempted": False, "notion_write_attempted": False}
    notion = client or NotionClient(config.token or "")
    meeting_key = str(outcome.get("meeting_key") or "")
    existing = _find_page_by_meeting_key(notion, config.database_id or "", meeting_key)
    if not existing:
        return {"status": "skipped", "reason": "meeting_prep_page_not_found", "meeting_key": meeting_key, "outcome": summary, "calendar_write_attempted": False, "notion_write_attempted": False}
    props = meeting_outcome_to_properties(outcome, task_result or {}, memory_result or {})
    updated = notion.update_page(existing["id"], properties=props)
    return {"status": "updated", "reason": "meeting_prep_outcome_updated", "page_id": updated.get("id") or existing.get("id"), "meeting_key": meeting_key, "outcome": summary, "calendar_write_attempted": False, "notion_write_attempted": True}


def meeting_prep_to_properties(meeting: dict[str, Any], brief_payload: dict[str, Any], *, discord_status: str) -> dict[str, Any]:
    brief = brief_payload.get("brief") or {}
    refs = ", ".join(str(ref) for ref in (brief_payload.get("source_refs") or [])[:20])
    people = ", ".join(str(item.get("name") or item.get("domain") or "") for item in (meeting.get("participants") or [])[:12] if isinstance(item, dict))
    return {
        "Title": {"title": [{"type": "text", "text": {"content": _safe_text(meeting.get("title"), 180)}}]},
        "Meeting date": _date_prop(str(meeting.get("date") or meeting.get("start_local") or "")[:10]),
        "Meeting key": _rich_text_prop(meeting.get("meeting_key")),
        "Calendar event ref": _rich_text_prop(meeting.get("calendar_event_ref")),
        "Status": _select_prop("Ready" if brief_payload.get("status") == "ok" else "Skipped"),
        "Context confidence": {"number": float(brief_payload.get("confidence") or 0)},
        "People / companies": _rich_text_prop(people),
        "Source refs": _rich_text_prop(refs),
        "Dusan notes": _rich_text_prop("\n".join(brief.get("dusan_notes") or [])),
        "Brief": _rich_text_prop(brief_payload.get("discord_message") or brief.get("focus")),
        "Questions": _rich_text_prop("\n".join(brief.get("questions") or [])),
        "Open loops": _rich_text_prop("\n".join(brief.get("open_loops") or [])),
        "Discord status": _select_prop(discord_status),
        "Last sent at": _date_prop(date.today().isoformat() if discord_status in {"Sent", "Dry run"} else None),
        "Message hash": _rich_text_prop(brief_payload.get("message_sha256")),
        "Updated date": _date_prop(date.today().isoformat()),
    }


def meeting_outcome_to_properties(outcome: dict[str, Any], task_result: dict[str, Any], memory_result: dict[str, Any]) -> dict[str, Any]:
    task_refs = ", ".join(str(item.get("page_id") or item.get("dedupe_key") or "") for item in (task_result.get("task_refs") or [])[:12] if isinstance(item, dict))
    memory_refs = ", ".join(str(item.get("path") or item.get("note_type") or "") for item in (memory_result.get("memory_refs") or [])[:8] if isinstance(item, dict))
    status = "Applied" if task_result.get("status") == "ok" else "Manual review" if outcome.get("status") == "manual_review_required" else "Captured"
    return {
        "Outcome status": _select_prop(status),
        "Outcome summary": _rich_text_prop(_safe_outcome_text(outcome)),
        "Decisions": _rich_text_prop("\n".join(str(item) for item in (outcome.get("decisions") or [])[:8])),
        "Follow-up task refs": _rich_text_prop(task_refs),
        "Memory refs": _rich_text_prop(memory_refs),
        "Outcome hash": _rich_text_prop(outcome.get("outcome_hash")),
        "Outcome captured at": _date_prop(date.today().isoformat()),
        "Updated date": _date_prop(date.today().isoformat()),
    }


def _find_page_by_meeting_key(notion: Any, database_id: str, meeting_key: str) -> dict[str, Any] | None:
    payload = notion.query_database(database_id, {"filter": {"property": "Meeting key", "rich_text": {"equals": meeting_key}}, "page_size": 1})
    results = payload.get("results") or []
    return results[0] if results else None


def _write_state(path: Path, updates: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_json(path)
    payload.update(updates)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _rich_text_prop(value: Any) -> dict[str, Any]:
    text = _safe_text(value, 1800)
    return {"rich_text": ([{"type": "text", "text": {"content": text}}] if text else [])}


def _date_prop(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    return {"date": {"start": text}} if text else {"date": None}


def _select_prop(name: str | None) -> dict[str, Any]:
    return {"select": {"name": str(name or "Not sent")}}


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_summary(meeting: dict[str, Any], brief_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "meeting_key": meeting.get("meeting_key"),
        "title": _safe_text(meeting.get("title"), 180),
        "start_local": meeting.get("start_local"),
        "status": brief_payload.get("status"),
        "message_sha256": brief_payload.get("message_sha256"),
    }


def _safe_outcome_summary(outcome: dict[str, Any], task_result: dict[str, Any], memory_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "meeting_key": outcome.get("meeting_key"),
        "title": _safe_text(outcome.get("title"), 180),
        "status": outcome.get("status"),
        "outcome_hash": outcome.get("outcome_hash"),
        "decision_count": outcome.get("decision_count"),
        "follow_up_count": outcome.get("follow_up_count"),
        "task_ref_count": len(task_result.get("task_refs") or []),
        "memory_ref_count": len(memory_result.get("memory_refs") or []),
    }


def _safe_outcome_text(outcome: dict[str, Any]) -> str:
    parts = []
    if outcome.get("decisions"):
        parts.append("Decisions: " + "; ".join(str(item) for item in (outcome.get("decisions") or [])[:3]))
    if outcome.get("follow_up_tasks"):
        parts.append("Follow-ups: " + "; ".join(str((item or {}).get("title") or "") for item in (outcome.get("follow_up_tasks") or [])[:5] if isinstance(item, dict)))
    if outcome.get("relationship_updates"):
        parts.append("Relationship: " + "; ".join(str(item) for item in (outcome.get("relationship_updates") or [])[:3]))
    return _safe_text(" | ".join(parts) or outcome.get("reason") or "Meeting outcome captured", 1800)


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or ensure Notion meeting prep setup.")
    parser.add_argument("--ensure-schema", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = ensure_meeting_prep_database_schema(live=args.live) if args.ensure_schema else meeting_prep_notion_health()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting prep Notion: {payload.get('status')}")
    return 0 if payload.get("status") in {"ok", "created", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
