#!/usr/bin/env python3
"""Dry-run task focus calendar proposal engine."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_calendar_dry_run import build_calendar_dry_run
from notion_task_manager import list_open_tasks


TIMEZONE = "Europe/Prague"
PREFERRED_WINDOW_START = "15:00"
PREFERRED_WINDOW_END = "17:30"
FALLBACK_WINDOW_START = "12:00"
FALLBACK_WINDOW_END = "19:30"
BOOKING_REASON = "Focused time for Rocky-tracked personal task"


def build_task_focus_proposals(
    *,
    planning_date: str | date | None = None,
    tasks: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
    existing_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    base = {
        "mode": "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": planning_day.isoformat(),
        "timezone": TIMEZONE,
        "calendar_write_attempted": False,
        "proposals": [],
    }
    if planning_day.weekday() >= 4:
        return {**base, "status": "skipped_weekend_target", "reason": "proactive_booking_blocked_on_friday_saturday_sunday"}
    if tasks is None:
        listed = list_open_tasks()
        if listed.get("status") != "ok":
            return {**base, "status": "blocked", "reason": str(listed.get("reason") or "notion_task_list_failed")}
        tasks = listed.get("tasks") or []
    eligible = [_task for _task in tasks if _eligible_for_focus(_task)]
    if not eligible:
        return {**base, "status": "skipped_no_focus_tasks", "reason": "no_eligible_task_focus_candidates", "task_count": len(tasks)}
    selected = sorted(eligible, key=_task_priority_sort)[0]
    duration = max(30, min(120, int(selected.get("estimated_effort_minutes") or 30)))
    duration = ((duration + 14) // 15) * 15
    day_events = existing_events
    if day_events is None:
        day_events = query_events(
            db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
            start=datetime.combine(planning_day, time(0, 0)),
            end=datetime.combine(planning_day, time(23, 59, 59)),
            include_all_day=False,
        )
    duplicate = _same_day_task_duplicate(day_events, planning_day, selected)
    if duplicate:
        return {
            **base,
            "status": "skipped_duplicate",
            "reason": "duplicate_rocky_task_focus_block",
            "selected_task": _safe_task(selected),
            "skipped_count": 1,
        }
    proposal = _proposal_for_window(
        selected,
        planning_day=planning_day,
        window_start=PREFERRED_WINDOW_START,
        window_end=PREFERRED_WINDOW_END,
        duration_minutes=duration,
        existing_events=day_events,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
    )
    proposals = [proposal]
    if proposal.get("reason") == "no_available_slot":
        proposal = _proposal_for_window(
            selected,
            planning_day=planning_day,
            window_start=FALLBACK_WINDOW_START,
            window_end=FALLBACK_WINDOW_END,
            duration_minutes=duration,
            existing_events=day_events,
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
        )
        proposals.append(proposal)
    status = "proposal" if proposal.get("status") == "proposal" else "blocked"
    return {
        **base,
        "status": status,
        "reason": None if status == "proposal" else str(proposal.get("reason") or "task_focus_proposal_blocked"),
        "selected_task": _safe_task(selected),
        "selected_proposal": proposal,
        "proposals": proposals,
        "idempotency_key": proposal.get("idempotency_key"),
        "audit_id": proposal.get("audit_id"),
        "blocked_count": 1 if status == "blocked" else 0,
    }


def _proposal_for_window(
    task: dict[str, Any],
    *,
    planning_day: date,
    window_start: str,
    window_end: str,
    duration_minutes: int,
    existing_events: list[dict[str, Any]],
    db_path: str | Path | None,
    ledger_path: str | Path | None,
    write_audit: bool,
) -> dict[str, Any]:
    metadata = {
        "task_id": task.get("rocky_task_id") or task.get("page_id") or "",
        "priority": task.get("priority") or "Normal",
        "estimated_effort_minutes": duration_minutes,
        "dedupe_key": task.get("dedupe_key") or "",
    }
    proposal = build_calendar_dry_run(
        kind="task_focus",
        day=planning_day.isoformat(),
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        label=str(task.get("title") or "task")[:80],
        reason=BOOKING_REASON,
        source_refs=[task.get("source_ref") or task.get("dedupe_key") or "notion-task"],
        confidence="high" if float(task.get("confidence") or 0) >= 0.8 else "medium",
        metadata_extra=metadata,
        db_path=db_path,
        existing_events=existing_events,
        ledger_path=ledger_path,
        record_audit=write_audit,
    )
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "idempotency_key": proposal.get("idempotency_key"),
        "audit_id": proposal.get("audit_id"),
        "proposal": _safe_calendar_proposal(proposal),
        "window_start": window_start,
        "window_end": window_end,
        "duration_minutes": duration_minutes,
        "selected_task": _safe_task(task),
        "calendar_write_attempted": False,
    }


def _eligible_for_focus(task: dict[str, Any]) -> bool:
    if str(task.get("status") or "Open") not in {"Open", "Scheduled", "Candidate"}:
        return False
    if not bool(task.get("requires_dusan_action", True)):
        return False
    if str(task.get("calendar_block_status") or "None") == "Scheduled":
        return False
    priority = str(task.get("priority") or "Normal")
    confidence = float(task.get("confidence") or 0)
    effort = int(task.get("estimated_effort_minutes") or 0)
    return (priority in {"High", "Urgent"} or task.get("due_date")) and confidence >= 0.75 and effort >= 30


def _task_priority_sort(task: dict[str, Any]) -> tuple[int, str, str]:
    priority_rank = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}
    return (priority_rank.get(str(task.get("priority") or "Normal"), 2), str(task.get("due_date") or "9999-12-31"), str(task.get("title") or ""))


def _same_day_task_duplicate(events: list[dict[str, Any]], planning_day: date, task: dict[str, Any]) -> bool:
    day_prefix = planning_day.isoformat()
    title = str(task.get("title") or "")
    for event in events:
        if event.get("all_day"):
            continue
        if not str(event.get("start_local") or "").startswith(day_prefix):
            continue
        summary = str(event.get("summary") or "")
        description = str(event.get("description") or "")
        if summary.startswith("Rocky: Task focus") and (title[:40] in summary or str(task.get("dedupe_key") or "") in description):
            return True
    return False


def _safe_calendar_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "title": proposal.get("title"),
        "start": proposal.get("start"),
        "end": proposal.get("end"),
        "duration_minutes": proposal.get("duration_minutes"),
        "confidence": proposal.get("confidence"),
        "calendar_write_attempted": bool(proposal.get("calendar_write_attempted")),
    }


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": task.get("page_id"),
        "rocky_task_id": task.get("rocky_task_id"),
        "title": task.get("title"),
        "priority": task.get("priority"),
        "due_date": task.get("due_date"),
        "confidence": task.get("confidence"),
        "estimated_effort_minutes": task.get("estimated_effort_minutes"),
        "dedupe_key": task.get("dedupe_key"),
        "source_ref": task.get("source_ref"),
    }


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()
