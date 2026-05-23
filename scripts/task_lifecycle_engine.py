#!/usr/bin/env python3
"""Task lifecycle and reminder metadata updates for Rocky."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from notion_task_manager import TERMINAL_TASK_STATUSES, list_open_tasks, update_task_reminder_metadata

POLICY_VERSION = "rocky-task-lifecycle-v1"
WORKFLOW = "task_lifecycle_engine"
CADENCE_WORKING_DAYS = {"Urgent": 1, "High": 1, "Normal": 2, "Low": 5, "Waiting": 3}


def run_task_lifecycle(
    *,
    today: str | date | None = None,
    tasks: list[dict[str, Any]] | None = None,
    live: bool = False,
    client: Any | None = None,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
) -> dict[str, Any]:
    day = _parse_date(today) if today else date.today()
    if tasks is None:
        listed = list_open_tasks()
        if listed.get("status") != "ok":
            return {"status": "blocked", "reason": listed.get("reason") or "notion_task_list_failed", "calendar_write_attempted": False, "notion_write_attempted": False}
        tasks = listed.get("tasks") or []
    due = []
    terminal_skipped = 0
    for task in tasks:
        if _is_terminal(task):
            terminal_skipped += 1
            continue
        if _is_due(task, day):
            due.append(task)
    base = {
        "workflow": WORKFLOW,
        "target_date": day.isoformat(),
        "due_count": len(due),
        "terminal_skipped_count": terminal_skipped,
        "calendar_write_attempted": False,
    }
    if not due:
        return {**base, "status": "skipped_no_due_tasks", "reason": "no_due_lifecycle_updates", "notion_write_attempted": False, "updated_count": 0}
    if not live:
        return {**base, "status": "dry_run", "reason": "live_flag_not_supplied", "would_update_count": len(due), "notion_write_attempted": False}
    updates = []
    for task in due:
        page_id = str(task.get("page_id") or "").strip()
        if not page_id:
            updates.append({"status": "blocked", "reason": "task_page_id_missing", "dedupe_key": task.get("dedupe_key")})
            continue
        reminder_count = int(task.get("reminder_count") or 0) + 1
        next_date = next_reminder_date_for(task, today=day)
        result = update_task_reminder_metadata(
            page_id=page_id,
            last_reminded_date=day.isoformat(),
            next_reminder_date=next_date,
            reminder_count=reminder_count,
            lifecycle_reason="reminder_metadata_updated",
            client=client,
        )
        updates.append({"page_id": result.get("page_id"), "status": result.get("status"), "reason": result.get("reason"), "next_reminder_date": next_date})
    status = "updated" if any(item.get("status") == "updated" for item in updates) else "blocked"
    payload = {
        **base,
        "status": status,
        "reason": "task_lifecycle_updated" if status == "updated" else "task_lifecycle_update_blocked",
        "updated_count": sum(1 for item in updates if item.get("status") == "updated"),
        "updates": updates,
        "notion_write_attempted": True,
    }
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="task.lifecycle_updated",
            workflow=WORKFLOW,
            idempotency_key=f"task-lifecycle:{day.isoformat()}",
            policy_version=POLICY_VERSION,
            decision="completed" if status == "updated" else "blocked",
            reason=str(payload["reason"]),
            sources=["notion:task-spine"],
            artifacts={"due_count": len(due), "updated_count": payload["updated_count"]},
        )
        payload["audit_id"] = event.audit_id
    return payload


def next_reminder_date_for(task: dict[str, Any], *, today: str | date | None = None) -> str:
    day = _parse_date(today) if today else date.today()
    priority = str(task.get("priority") or "Normal").title()
    status = str(task.get("status") or "Open").title()
    days = CADENCE_WORKING_DAYS.get(status) if status == "Waiting" else CADENCE_WORKING_DAYS.get(priority, 2)
    return _add_working_days(day, days).isoformat()


def _is_due(task: dict[str, Any], day: date) -> bool:
    if not bool(task.get("requires_dusan_action", True)):
        return False
    next_reminder = _parse_date(task.get("next_reminder_date")) if task.get("next_reminder_date") else day
    return next_reminder <= day


def _is_terminal(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "").title() in TERMINAL_TASK_STATUSES


def _add_working_days(day: date, count: int) -> date:
    current = day
    remaining = max(0, int(count))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 4:
            remaining -= 1
    return current


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky task lifecycle updates.")
    parser.add_argument("--today")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_task_lifecycle(today=args.today, live=args.live, ledger_path=args.ledger_path, write_audit=args.write_audit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task lifecycle: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"updated", "dry_run", "skipped_no_due_tasks"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
