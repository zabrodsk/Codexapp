#!/usr/bin/env python3
"""Read-only signal collection for Rocky's weekly personal review."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_preference_models import learning_summary
from assistant_scheduler_state import AssistantSchedulerState
from coding_work_briefing_builder import build_coding_work_briefing
from notion_task_manager import list_open_tasks
from task_command_reconciler import recent_task_commands
from weekly_calendar_hygiene import inspect_weekly_calendar_hygiene

TIMEZONE = "Europe/Prague"
WORKFLOW = "weekly_personal_signal_collector"
STATE_FILES = {
    "training_calendar_booking": Path("/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json"),
    "email_triage_booking": Path("/Users/clawdbot/.openclaw/state/email_triage_scheduler.json"),
    "task_spine": Path("/Users/clawdbot/.openclaw/state/task_spine_scheduler.json"),
    "coding_work_briefing": Path("/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json"),
    "task_command_capture": Path("/Users/clawdbot/.openclaw/state/task_command_capture_scheduler.json"),
    "daily_personal_briefing": Path("/Users/clawdbot/.openclaw/state/daily_personal_briefing_scheduler.json"),
    "assistant_learning": Path("/Users/clawdbot/.openclaw/state/assistant_learning_scheduler.json"),
}
SENSITIVE_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def collect_weekly_personal_signals(
    *,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
    scheduler_states: dict[str, dict[str, Any]] | None = None,
    tasks_payload: dict[str, Any] | None = None,
    command_payload: dict[str, Any] | None = None,
    coding_payload: dict[str, Any] | None = None,
    learning_payload: dict[str, Any] | None = None,
    hygiene_payload: dict[str, Any] | None = None,
    include_live_tasks: bool = True,
    include_live_commands: bool = True,
    include_live_coding: bool = True,
    include_learning: bool = True,
    learning_db_path: str | Path | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    week_start = planning_day - timedelta(days=planning_day.weekday())
    week_end = week_start + timedelta(days=6)
    previous_week_start = week_start - timedelta(days=7)
    errors: list[dict[str, Any]] = []

    events = calendar_events
    if events is None:
        try:
            events = query_events(db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH, start=datetime.combine(week_start, time(0, 0)), end=datetime.combine(week_start + timedelta(days=14), time(0, 0)), include_all_day=True)
        except Exception as exc:
            events = []
            errors.append({"source": "calendar", "reason": "calendar_read_failed", "error_hash": _hash_text(str(exc))})

    states = scheduler_states if scheduler_states is not None else _read_scheduler_states()
    dead_letters = _dead_letters(scheduler_db_path, errors)
    tasks_raw = tasks_payload
    if tasks_raw is None and include_live_tasks:
        try:
            tasks_raw = list_open_tasks(limit=100)
        except Exception as exc:
            tasks_raw = {"status": "degraded", "reason": "notion_task_list_failed", "tasks": []}
            errors.append({"source": "tasks", "reason": "notion_task_list_failed", "error_hash": _hash_text(str(exc))})
    commands_raw = command_payload
    if commands_raw is None and include_live_commands:
        try:
            commands_raw = recent_task_commands(limit=30)
        except Exception as exc:
            commands_raw = {"status": "degraded", "reason": "task_command_recent_failed", "commands": []}
            errors.append({"source": "commands", "reason": "task_command_recent_failed", "error_hash": _hash_text(str(exc))})
    coding_raw = coding_payload
    if coding_raw is None and include_live_coding:
        try:
            coding_raw = build_coding_work_briefing(planning_date=planning_day)
        except Exception as exc:
            coding_raw = {"status": "degraded", "reason": "coding_briefing_failed", "selected_focus_items": []}
            errors.append({"source": "coding", "reason": "coding_briefing_failed", "error_hash": _hash_text(str(exc))})
    learning_raw = learning_payload
    if learning_raw is None and include_learning:
        try:
            learning_raw = learning_summary(db_path=learning_db_path)
        except Exception as exc:
            learning_raw = {"status": "degraded", "reason": "learning_summary_failed"}
            errors.append({"source": "learning", "reason": "learning_summary_failed", "error_hash": _hash_text(str(exc))})
    hygiene_raw = hygiene_payload
    if hygiene_raw is None:
        try:
            hygiene_raw = inspect_weekly_calendar_hygiene(start_date=week_start, days=14, db_path=db_path, events=events, mark_stale=False, live=False)
        except Exception as exc:
            hygiene_raw = {"status": "degraded", "reason": "calendar_hygiene_failed", "issue_count": 0}
            errors.append({"source": "calendar_hygiene", "reason": "calendar_hygiene_failed", "error_hash": _hash_text(str(exc))})

    payload = {
        "status": "ok" if not errors else "degraded",
        "workflow": WORKFLOW,
        "planning_date": planning_day.isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "week_label": _iso_week_label(week_start),
        "previous_week_start": previous_week_start.isoformat(),
        "timezone": TIMEZONE,
        "calendar": _calendar_summary(events or [], week_start=week_start, days=14),
        "training": _state_lane("training_calendar_booking", states),
        "email": _state_lane("email_triage_booking", states),
        "tasks": _task_summary(tasks_raw or {"tasks": []}),
        "coding": _coding_summary(coding_raw or {}),
        "command_activity": _command_summary(commands_raw or {}),
        "learning": _learning_summary(learning_raw or {}),
        "scheduler": _scheduler_summary(states),
        "dead_letters": dead_letters,
        "calendar_hygiene": _safe_hygiene(hygiene_raw or {}),
        "errors": errors,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    return _redact_payload(payload)


def _calendar_summary(events: list[dict[str, Any]], *, week_start: date, days: int) -> dict[str, Any]:
    safe_events = [_safe_event(event) for event in events]
    day_rows = []
    for offset in range(days):
        day = week_start + timedelta(days=offset)
        day_events = [event for event in safe_events if str(event.get("start_local") or "").startswith(day.isoformat())]
        free = _free_after_noon(day_events, day)
        day_rows.append({"date": day.isoformat(), "event_count": len(day_events), "rocky_block_count": sum(1 for e in day_events if str(e.get("summary") or "").startswith("Rocky:")), "free_minutes_after_noon": free["free_minutes"], "max_focus_window_minutes": free["max_window"]})
    return {"status": "ok", "event_count": len(safe_events), "rocky_block_count": sum(1 for e in safe_events if str(e.get("summary") or "").startswith("Rocky:")), "days": day_rows, "events": safe_events[:30]}


def _free_after_noon(events: list[dict[str, Any]], day: date) -> dict[str, int]:
    start = datetime.combine(day, time(12, 30)); end = datetime.combine(day, time(19, 30)); cursor = start; busy = []
    for event in events:
        if event.get("all_day"):
            continue
        ev_start = _parse_event_dt(event.get("start_local")); ev_end = _parse_event_dt(event.get("end_local"))
        if not ev_start or not ev_end or ev_end <= start or ev_start >= end:
            continue
        busy.append((max(ev_start, start), min(ev_end, end)))
    busy.sort(); windows = []
    for ev_start, ev_end in busy:
        if ev_start > cursor:
            windows.append(int((ev_start - cursor).total_seconds() // 60))
        cursor = max(cursor, ev_end)
    if cursor < end:
        windows.append(int((end - cursor).total_seconds() // 60))
    return {"free_minutes": sum(windows), "max_window": max(windows or [0])}


def _task_summary(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = payload.get("tasks") or []
    return {"status": payload.get("status") or "ok", "open_count": len(tasks), "urgent_count": sum(1 for t in tasks if str(t.get("priority")) == "Urgent"), "high_count": sum(1 for t in tasks if str(t.get("priority")) == "High"), "top_tasks": [_safe_task(task) for task in tasks[:12]]}


def _coding_summary(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("selected_focus_items") or payload.get("top_items") or []
    return {"status": payload.get("status") or "unknown", "reason": _safe_text(payload.get("reason"), 120), "selected_count": len(items), "top_items": [_safe_coding_item(item) for item in items[:5]]}


def _command_summary(payload: dict[str, Any]) -> dict[str, Any]:
    commands = payload.get("commands") or []
    return {"status": payload.get("status") or "ok", "command_count": len(commands), "recent": [{"source": c.get("source"), "status": c.get("status"), "preview": _safe_text(c.get("preview") or c.get("text_preview"), 120)} for c in commands[:8]]}


def _learning_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": payload.get("status") or "unknown", "active_bounded_count": int(payload.get("active_bounded_count") or 0), "proposal_count": int(payload.get("proposal_count") or 0), "outcome_count": int(payload.get("outcome_count") or 0)}


def _state_lane(job: str, states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    state = states.get(job) or {}
    return {"status": state.get("last_status") or "unknown", "target_date": state.get("target_date"), "reason": _safe_text(state.get("reason"), 160), "created_count": state.get("created_count", 0), "skipped_count": state.get("skipped_count", 0), "blocked_count": state.get("blocked_count", 0), "error_hash": state.get("error_hash")}


def _scheduler_summary(states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    problem = [name for name, state in states.items() if state.get("error_hash") or str(state.get("last_status") or "") in {"failed", "blocked", "manual_review_required"}]
    return {"status": "ok" if not problem else "degraded", "problem_count": len(problem), "problem_jobs": problem, "states": {name: _state_lane(name, states) for name in states}}


def _dead_letters(scheduler_db_path: str | Path | None, errors: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        rows = AssistantSchedulerState(scheduler_db_path).list_dead_letters(status="open", limit=20)
    except Exception as exc:
        errors.append({"source": "dead_letters", "reason": "dead_letters_read_failed", "error_hash": _hash_text(str(exc))})
        rows = []
    return {"status": "ok", "open_count": len(rows), "items": [{"job_name": r.get("job_name"), "failure_class": r.get("failure_class"), "safe_summary": _safe_text(r.get("safe_summary"), 180), "recovery_hint": _safe_text(r.get("recovery_hint"), 180)} for r in rows[:8]]}


def _safe_hygiene(payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": payload.get("status"), "reason": payload.get("reason"), "issue_count": payload.get("issue_count", 0), "duplicate_count": len(payload.get("duplicate_rocky_blocks") or []), "stale_count": len(payload.get("stale_state_candidates") or []), "orphan_count": len(payload.get("orphan_rocky_events") or []), "weekend_violation_count": len(payload.get("weekend_policy_violations") or []), "overbooked_day_count": len(payload.get("overbooked_days") or []), "no_focus_day_count": len(payload.get("no_realistic_focus_days") or []), "overbooked_days": payload.get("overbooked_days") or [], "no_realistic_focus_days": payload.get("no_realistic_focus_days") or []}


def _read_scheduler_states() -> dict[str, dict[str, Any]]:
    states = {}
    for name, path in STATE_FILES.items():
        try:
            states[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"last_status": "missing"}
        except Exception as exc:
            states[name] = {"last_status": "degraded", "reason": "state_read_failed", "error_hash": _hash_text(str(exc))}
    return states


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"summary": _safe_text(event.get("summary"), 160), "start_local": event.get("start_local"), "end_local": event.get("end_local"), "all_day": bool(event.get("all_day")), "calendar": _safe_text(event.get("calendar"), 80), "location": _safe_text(event.get("location"), 120)}


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    return {"title": _safe_text(task.get("title"), 160), "priority": task.get("priority"), "status": task.get("status"), "due_date": task.get("due_date"), "confidence": task.get("confidence"), "estimated_effort_minutes": task.get("estimated_effort_minutes"), "page_id": task.get("page_id"), "source_ref": task.get("source_ref"), "dedupe_key": task.get("dedupe_key")}


def _safe_coding_item(item: dict[str, Any]) -> dict[str, Any]:
    return {"project": _safe_text(item.get("project"), 120), "title": _safe_text(item.get("title"), 160), "priority": item.get("priority"), "confidence": item.get("confidence"), "recommended_next_step": _safe_text(item.get("recommended_next_step"), 240), "source_refs": item.get("source_refs") or []}


def _parse_event_dt(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _safe_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 800)
    return value


def _hash_text(value: Any) -> str:
    safe = SENSITIVE_RE.sub("[redacted]", str(value or ""))
    return hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]
