#!/usr/bin/env python3
"""Apply meeting follow-up tasks through Rocky's task identity and Notion rails."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from notion_task_manager import ensure_task_database_schema, list_open_tasks, upsert_task
from task_identity_resolver import resolve_task_identities


def apply_meeting_outcome_tasks(
    outcome: dict[str, Any],
    *,
    live: bool = False,
    existing_tasks: list[dict[str, Any]] | None = None,
    list_tasks_func: Callable[..., dict[str, Any]] | None = None,
    upsert_func: Callable[..., dict[str, Any]] | None = None,
    notion_client: Any | None = None,
    notion_config: Any | None = None,
) -> dict[str, Any]:
    candidates = [task for task in outcome.get("follow_up_tasks") or [] if _eligible(task)]
    resolved = resolve_task_identities(candidates, existing_tasks=existing_tasks or _existing_tasks(list_tasks_func), today=date.today().isoformat())
    if not live:
        return {
            "status": "dry_run",
            "reason": "live_flag_not_supplied",
            "candidate_count": len(candidates),
            "resolved": resolved,
            "created_count": resolved.get("create_count", 0),
            "updated_count": resolved.get("update_count", 0),
            "task_refs": [],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    schema = ensure_task_database_schema(live=True, config=notion_config, client=notion_client)
    if schema.get("status") not in {"ok", "created"}:
        return {**schema, "status": "blocked", "reason": schema.get("reason") or "notion_task_schema_blocked", "calendar_write_attempted": False}
    task_refs: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    writer = upsert_func or upsert_task
    for item in resolved.get("results") or []:
        action = item.get("action")
        if action not in {"create", "update"}:
            results.append({"action": action, "reason": item.get("reason"), "dedupe_key": item.get("dedupe_key")})
            continue
        task = item.get("task") or {}
        result = writer(task, live=True, config=notion_config, client=notion_client)
        results.append({"action": action, "status": result.get("status"), "reason": result.get("reason"), "page_id": result.get("page_id"), "dedupe_key": result.get("dedupe_key")})
        if result.get("page_id"):
            task_refs.append({"page_id": result.get("page_id"), "dedupe_key": result.get("dedupe_key"), "action": action, "title": task.get("title")})
    return {
        "status": "ok",
        "reason": "meeting_outcome_tasks_applied",
        "candidate_count": len(candidates),
        "resolved": resolved,
        "results": results,
        "task_refs": task_refs,
        "created_count": sum(1 for item in results if item.get("action") == "create" and item.get("status") in {"created", "updated"}),
        "updated_count": sum(1 for item in results if item.get("action") == "update" and item.get("status") in {"created", "updated"}),
        "calendar_write_attempted": False,
        "notion_write_attempted": bool(task_refs),
    }


def _existing_tasks(list_tasks_func: Callable[..., dict[str, Any]] | None) -> list[dict[str, Any]]:
    try:
        payload = (list_tasks_func or list_open_tasks)(limit=100)
    except Exception:
        return []
    return payload.get("tasks") or [] if payload.get("status") == "ok" else []


def _eligible(task: dict[str, Any]) -> bool:
    return bool(task.get("requires_dusan_action", True)) and float(task.get("confidence") or 0) >= 0.7 and bool(task.get("title"))
