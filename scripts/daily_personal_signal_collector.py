#!/usr/bin/env python3
"""Read-only signal collection for Rocky's daily personal assistant briefing."""
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
from assistant_scheduler_state import AssistantSchedulerState
from coding_focus_proposal_engine import build_coding_focus_proposals
from coding_work_briefing_builder import build_coding_work_briefing
from email_triage_proposal_engine import build_email_triage_proposals
from notion_task_manager import list_open_tasks
from task_command_reconciler import recent_task_commands
from task_focus_proposal_engine import build_task_focus_proposals

TIMEZONE = "Europe/Prague"
WORKFLOW = "daily_personal_signal_collector"
STATE_FILES = {
    "training_calendar_booking": Path("/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json"),
    "email_triage_booking": Path("/Users/clawdbot/.openclaw/state/email_triage_scheduler.json"),
    "task_spine": Path("/Users/clawdbot/.openclaw/state/task_spine_scheduler.json"),
    "coding_work_briefing": Path("/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json"),
    "task_command_capture": Path("/Users/clawdbot/.openclaw/state/task_command_capture_scheduler.json"),
}
SENSITIVE_TEXT_RE = re.compile(
    r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def collect_daily_personal_signals(
    *,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
    scheduler_states: dict[str, dict[str, Any]] | None = None,
    email_payload: dict[str, Any] | None = None,
    task_payload: dict[str, Any] | None = None,
    coding_payload: dict[str, Any] | None = None,
    coding_proposals_payload: dict[str, Any] | None = None,
    task_focus_payload: dict[str, Any] | None = None,
    command_payload: dict[str, Any] | None = None,
    dead_letters: list[dict[str, Any]] | None = None,
    include_live_email: bool = True,
    include_live_tasks: bool = True,
    include_live_coding: bool = True,
    include_live_commands: bool = True,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    is_weekday = planning_day.weekday() < 5
    booking_allowed = planning_day.weekday() < 4
    policy_reason = "booking_allowed_monday_through_thursday" if booking_allowed else "proactive_booking_blocked_on_friday_saturday_sunday"
    errors: list[dict[str, Any]] = []

    events_raw = calendar_events
    if events_raw is None:
        try:
            events_raw = query_events(
                db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
                start=datetime.combine(planning_day, time(0, 0)),
                end=datetime.combine(planning_day, time(23, 59, 59)),
                include_all_day=True,
            )
        except Exception as exc:
            events_raw = []
            errors.append({"source": "calendar", "reason": "calendar_read_failed", "error_hash": _hash_text(str(exc))})
    calendar = _calendar_summary(events_raw or [], planning_day)

    states = scheduler_states if scheduler_states is not None else _read_scheduler_states()
    scheduler = _scheduler_summary(states)

    dead_raw = dead_letters
    if dead_raw is None:
        try:
            dead_raw = AssistantSchedulerState(scheduler_db_path).list_dead_letters(status="open", limit=10)
        except Exception as exc:
            dead_raw = []
            errors.append({"source": "dead_letters", "reason": "dead_letters_read_failed", "error_hash": _hash_text(str(exc))})
    dead = _dead_letter_summary(dead_raw or [])

    email_raw = email_payload
    if email_raw is None and include_live_email:
        try:
            email_raw = build_email_triage_proposals(planning_date=planning_day, db_path=db_path, write_audit=False)
        except Exception as exc:
            email_raw = {"status": "blocked", "reason": "email_triage_signal_failed", "error_hash": _hash_text(str(exc))}
            errors.append({"source": "email", "reason": "email_triage_signal_failed", "error_hash": _hash_text(str(exc))})
    email = _email_summary(email_raw or {}, states.get("email_triage_booking") or {})

    tasks_raw = task_payload
    if tasks_raw is None and include_live_tasks:
        try:
            tasks_raw = list_open_tasks(limit=50)
        except Exception as exc:
            tasks_raw = {"status": "blocked", "reason": "notion_task_list_failed", "error_hash": _hash_text(str(exc)), "tasks": []}
            errors.append({"source": "tasks", "reason": "notion_task_list_failed", "error_hash": _hash_text(str(exc))})
    tasks = _task_summary(tasks_raw or {"status": "skipped", "tasks": []}, planning_day)

    coding_raw = coding_payload
    if coding_raw is None and include_live_coding:
        try:
            coding_raw = build_coding_work_briefing(planning_date=planning_day)
        except Exception as exc:
            coding_raw = {"status": "blocked", "reason": "coding_briefing_signal_failed", "error_hash": _hash_text(str(exc)), "selected_focus_items": []}
            errors.append({"source": "coding", "reason": "coding_briefing_signal_failed", "error_hash": _hash_text(str(exc))})
    coding_props = coding_proposals_payload
    if coding_props is None and coding_raw and include_live_coding:
        try:
            coding_props = build_coding_focus_proposals(planning_date=planning_day, briefing_payload=coding_raw, existing_events=events_raw or [], write_audit=False, db_path=db_path)
        except Exception as exc:
            coding_props = {"status": "blocked", "reason": "coding_focus_proposal_failed", "error_hash": _hash_text(str(exc)), "proposals": []}
    coding = _coding_summary(coding_raw or {}, coding_props or {})

    task_focus_raw = task_focus_payload
    if task_focus_raw is None and include_live_tasks:
        try:
            task_focus_raw = build_task_focus_proposals(planning_date=planning_day, tasks=tasks_raw.get("tasks") if isinstance(tasks_raw, dict) else [], existing_events=events_raw or [], write_audit=False, db_path=db_path)
        except Exception as exc:
            task_focus_raw = {"status": "blocked", "reason": "task_focus_proposal_failed", "error_hash": _hash_text(str(exc))}
    task_focus = _safe_mapping(task_focus_raw or {}, keys=("status", "reason", "idempotency_key", "blocked_count", "skipped_count"))

    command_raw = command_payload
    if command_raw is None and include_live_commands:
        try:
            command_raw = recent_task_commands(limit=10)
        except Exception as exc:
            command_raw = {"status": "blocked", "reason": "task_command_recent_failed", "error_hash": _hash_text(str(exc)), "commands": []}
            errors.append({"source": "commands", "reason": "task_command_recent_failed", "error_hash": _hash_text(str(exc))})
    command_activity = _command_summary(command_raw or {})

    payload = {
        "status": "ok" if not errors else "degraded",
        "workflow": WORKFLOW,
        "planning_date": planning_day.isoformat(),
        "timezone": TIMEZONE,
        "is_weekday_briefing_day": is_weekday,
        "booking_allowed_today": booking_allowed,
        "booking_policy_reason": policy_reason,
        "calendar": calendar,
        "training": _training_summary(calendar, states.get("training_calendar_booking") or {}),
        "email": email,
        "tasks": tasks,
        "task_focus": task_focus,
        "coding": coding,
        "command_activity": command_activity,
        "scheduler": scheduler,
        "dead_letters": dead,
        "errors": errors,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    return _redact_payload(payload)


def _calendar_summary(events: list[dict[str, Any]], planning_day: date) -> dict[str, Any]:
    sanitized = [_safe_event(event) for event in events]
    training_blocks = [event for event in sanitized if str(event.get("summary") or "").startswith("Rocky: Training")]
    rocky_blocks = [event for event in sanitized if str(event.get("summary") or "").startswith("Rocky:")]
    free_windows = _free_windows(sanitized, planning_day)
    return {
        "status": "ok",
        "event_count": len(sanitized),
        "events": sanitized[:20],
        "training_block_count": len(training_blocks),
        "rocky_block_count": len(rocky_blocks),
        "free_windows": free_windows,
        "free_minutes_after_noon": sum(int(item.get("minutes") or 0) for item in free_windows),
    }


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": _safe_text(event.get("summary"), 140),
        "start_local": event.get("start_local"),
        "end_local": event.get("end_local"),
        "all_day": bool(event.get("all_day")),
        "calendar": _safe_text(event.get("calendar"), 80),
        "location": _safe_text(event.get("location"), 120),
    }


def _free_windows(events: list[dict[str, Any]], planning_day: date) -> list[dict[str, Any]]:
    start = datetime.combine(planning_day, time(12, 30))
    end = datetime.combine(planning_day, time(19, 30))
    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        if event.get("all_day"):
            continue
        try:
            ev_start = _parse_event_dt(event.get("start_local"))
            ev_end = _parse_event_dt(event.get("end_local"))
        except Exception:
            continue
        if ev_end <= start or ev_start >= end:
            continue
        busy.append((max(start, ev_start), min(end, ev_end)))
    busy.sort()
    windows: list[dict[str, Any]] = []
    cursor = start
    for ev_start, ev_end in busy:
        if ev_start > cursor:
            minutes = int((ev_start - cursor).total_seconds() // 60)
            if minutes >= 30:
                windows.append({"start": cursor.strftime("%H:%M"), "end": ev_start.strftime("%H:%M"), "minutes": minutes})
        if ev_end > cursor:
            cursor = ev_end
    if cursor < end:
        minutes = int((end - cursor).total_seconds() // 60)
        if minutes >= 30:
            windows.append({"start": cursor.strftime("%H:%M"), "end": end.strftime("%H:%M"), "minutes": minutes})
    return windows


def _email_summary(payload: dict[str, Any], email_state: dict[str, Any]) -> dict[str, Any]:
    attention = payload.get("email_attention") or {}
    estimate = payload.get("estimate") or {}
    status = str(payload.get("status") or email_state.get("last_status") or "unknown")
    state_status = str(email_state.get("last_status") or "unknown")
    attention_count = int(attention.get("attention_count") or payload.get("attention_count") or 0)
    repair_candidate = status == "proposal" and state_status in {"missing", "failed", "blocked", "unknown", "degraded"}
    return {
        "status": status,
        "reason": _safe_text(payload.get("reason") or email_state.get("reason"), 120),
        "state_status": state_status,
        "attention_count": attention_count,
        "unread_count": int(attention.get("unread_count") or 0),
        "priority_buckets": attention.get("priority_buckets") or {},
        "estimated_minutes": int(estimate.get("estimated_minutes") or payload.get("estimated_minutes") or 0),
        "idempotency_key": payload.get("idempotency_key") or (payload.get("selected_proposal") or {}).get("idempotency_key"),
        "repair_candidate": bool(repair_candidate),
    }


def _task_summary(payload: dict[str, Any], planning_day: date) -> dict[str, Any]:
    tasks = payload.get("tasks") or []
    safe_tasks = [_safe_task(task) for task in tasks]
    urgent = [task for task in safe_tasks if task.get("priority") in {"Urgent", "High"}]
    due_soon = [task for task in safe_tasks if _due_soon(task.get("due_date"), planning_day)]
    top = sorted(safe_tasks, key=_task_sort_key)[:8]
    return {
        "status": payload.get("status") or "unknown",
        "reason": _safe_text(payload.get("reason"), 120),
        "open_count": len(safe_tasks),
        "urgent_count": len(urgent),
        "due_soon_count": len(due_soon),
        "top_tasks": top,
    }


def _coding_summary(payload: dict[str, Any], proposals: dict[str, Any]) -> dict[str, Any]:
    selected = payload.get("selected_focus_items") or []
    top = [_safe_work_item(item) for item in selected[:5]]
    proposal_keys = [item.get("idempotency_key") for item in proposals.get("proposals") or [] if item.get("status") == "proposal" and item.get("idempotency_key")]
    return {
        "status": payload.get("status") or "unknown",
        "reason": _safe_text(payload.get("reason"), 120),
        "work_item_count": int(payload.get("work_item_count") or len(payload.get("work_items") or [])),
        "selected_count": int(payload.get("selected_count") or len(selected)),
        "top_items": top,
        "selected_focus_items": top,
        "briefing_preview": _safe_text(payload.get("briefing"), 500),
        "proposal_status": proposals.get("status"),
        "proposal_idempotency_keys": proposal_keys or list(proposals.get("idempotency_keys") or []),
    }


def _training_summary(calendar: dict[str, Any], training_state: dict[str, Any]) -> dict[str, Any]:
    blocks = [event for event in calendar.get("events") or [] if str(event.get("summary") or "").startswith("Rocky: Training")]
    if blocks:
        summary = "; ".join(f"{event.get('summary')} {str(event.get('start_local') or '')[11:16]}-{str(event.get('end_local') or '')[11:16]}" for event in blocks[:2])
    else:
        summary = "No Rocky training block visible today."
    return {
        "status": training_state.get("last_status") or ("protected" if blocks else "unknown"),
        "today_block_count": len(blocks),
        "summary": _safe_text(summary, 240),
        "state": _safe_state(training_state),
    }


def _scheduler_summary(states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    safe_states = {key: _safe_state(value) for key, value in states.items()}
    problem = [key for key, value in safe_states.items() if str(value.get("last_status") or "ok") not in {"ok", "created", "skipped_duplicate", "skipped_no_workout", "skipped_no_attention_emails", "skipped_weekend_target", "source_ref_drift_verified"} and value.get("last_status")]
    return {"status": "ok" if not problem else "degraded", "problem_count": len(problem), "problem_jobs": problem, "states": safe_states}


def _command_summary(payload: dict[str, Any]) -> dict[str, Any]:
    commands = payload.get("commands") or []
    return {
        "status": payload.get("status") or "unknown",
        "command_count": int(payload.get("command_count") or len(commands)),
        "counts_by_status": payload.get("counts_by_status") or {},
        "counts_by_source": payload.get("counts_by_source") or {},
        "recent": [_safe_mapping(item, keys=("source_ref", "source_channel", "status", "text_preview", "task_title")) for item in commands[:5]],
    }


def _dead_letter_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok" if not rows else "attention_needed",
        "open_count": len(rows),
        "items": [_safe_mapping(row, keys=("dead_letter_id", "job_name", "failure_class", "safe_summary", "recovery_hint", "error_hash")) for row in rows[:8]],
    }


def _read_scheduler_states() -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name, path in STATE_FILES.items():
        if not path.exists():
            states[name] = {"last_status": "missing", "state_path": str(path)}
            continue
        try:
            states[name] = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            states[name] = {"last_status": "unreadable", "error_hash": _hash_text(str(exc)), "state_path": str(path)}
    return states


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
    keys = ("last_run_at", "last_status", "target_date", "reason", "created_count", "skipped_count", "blocked_count", "work_item_count", "commands_seen", "commands_processed", "tasks_created", "tasks_updated", "ack_sent_count", "ack_failed_count", "error_hash", "idempotency_key", "run_idempotency_key")
    return _safe_mapping(state or {}, keys=keys)


def _safe_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _safe_text(task.get("title"), 140),
        "priority": task.get("priority") or "Normal",
        "status": task.get("status") or "Open",
        "due_date": task.get("due_date"),
        "confidence": float(task.get("confidence") or 0),
        "estimated_effort_minutes": int(task.get("estimated_effort_minutes") or 0),
        "requires_dusan_action": bool(task.get("requires_dusan_action", True)),
        "page_id": task.get("page_id"),
        "source_ref": task.get("source_ref"),
        "dedupe_key": task.get("dedupe_key"),
    }


def _safe_work_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "work_item_id": item.get("work_item_id"),
        "project": _safe_text(item.get("project"), 100),
        "title": _safe_text(item.get("title"), 160),
        "priority": item.get("priority") or "Normal",
        "confidence": float(item.get("confidence") or 0),
        "recommended_next_step": _safe_text(item.get("recommended_next_step"), 220),
        "requires_dusan_decision": bool(item.get("requires_dusan_decision")),
        "source_refs": list(item.get("source_refs") or [])[:5],
    }


def _safe_mapping(value: dict[str, Any], *, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _redact_payload(value.get(key)) for key in keys if key in value}


def _task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    rank = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}
    return (rank.get(str(task.get("priority") or "Normal"), 2), str(task.get("due_date") or "9999-12-31"), str(task.get("title") or ""))


def _due_soon(value: Any, planning_day: date) -> bool:
    if not value:
        return False
    try:
        due = _parse_date(value)
    except Exception:
        return False
    return due <= planning_day + timedelta(days=1)


def _parse_event_dt(value: Any) -> datetime:
    text = str(value or "").replace("T", " ")[:19]
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _safe_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_TEXT_RE.sub("[redacted]", text)
    return text[:limit]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items() if str(key).lower() not in {"description", "raw", "body", "transcript", "notes"}}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 500)
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
