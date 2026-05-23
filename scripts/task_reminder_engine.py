#!/usr/bin/env python3
"""Task reminder selection and Discord notification dispatch for Rocky."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from assistant_notification_dispatcher import dispatch_failure_notification
from notion_task_manager import list_open_tasks


def run_task_reminders(
    *,
    today: str | date | None = None,
    tasks: list[dict[str, Any]] | None = None,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    ledger_path: str | None = None,
    scheduler_db_path: str | None = None,
) -> dict[str, Any]:
    day = _parse_date(today) if today else date.today()
    if tasks is None:
        listed = list_open_tasks()
        if listed.get("status") != "ok":
            return {"status": "blocked", "reason": str(listed.get("reason") or "notion_task_list_failed"), "reminders": []}
        tasks = listed.get("tasks") or []
    due = [_safe_task(task) for task in tasks if _should_remind(task, day)]
    payload = {
        "status": "ok" if due else "skipped_no_reminders",
        "reason": "task_reminders_due" if due else "no_task_reminders_due",
        "workflow": "task_reminder_engine",
        "target_date": day.isoformat(),
        "reminders": due,
        "reminder_count": len(due),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    if notify and due:
        payload["notification"] = dispatch_failure_notification(
            {
                "status": "manual_review_required",
                "reason": "task_reminders_due",
                "workflow": "task_reminder_engine",
                "target_date": day.isoformat(),
                "idempotency_key": f"task-reminders:{day.isoformat()}",
                "recommended_action": _render_reminder_action(due),
            },
            channel_id=notification_channel_id or "1485710572325703901",
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            dry_run=notification_dry_run,
        )
    return payload


def _should_remind(task: dict[str, Any], day: date) -> bool:
    if str(task.get("status") or "Open") not in {"Open", "Scheduled", "Waiting", "Candidate"}:
        return False
    if not bool(task.get("requires_dusan_action", True)):
        return False
    next_reminder = _parse_date(task.get("next_reminder_date")) if task.get("next_reminder_date") else day
    due_date = _parse_date(task.get("due_date")) if task.get("due_date") else None
    return next_reminder <= day or bool(due_date and due_date <= day + timedelta(days=1))


def _render_reminder_action(tasks: list[dict[str, Any]]) -> str:
    lines = [f"{len(tasks)} Rocky task reminder(s):"]
    for task in tasks[:8]:
        lines.append(f"- {task.get('priority')}: {task.get('title')}")
    return "\n".join(lines)


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": task.get("page_id"),
        "title": task.get("title"),
        "priority": task.get("priority"),
        "due_date": task.get("due_date"),
        "next_reminder_date": task.get("next_reminder_date"),
        "source_ref": task.get("source_ref"),
        "dedupe_key": task.get("dedupe_key"),
    }


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()
