#!/usr/bin/env python3
"""Deterministic weekly priority planning for Rocky's personal review."""
from __future__ import annotations

import hashlib
import re
from typing import Any

POLICY_VERSION = "rocky-weekly-priority-v1"
SENSITIVE_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def plan_weekly_priorities(signals: dict[str, Any]) -> dict[str, Any]:
    tasks = signals.get("tasks") or {}
    calendar = signals.get("calendar") or {}
    scheduler = signals.get("scheduler") or {}
    dead = signals.get("dead_letters") or {}
    hygiene = signals.get("calendar_hygiene") or {}
    coding = signals.get("coding") or {}
    learning = signals.get("learning") or {}

    risks = []
    open_loops = []
    do_first = []
    protect = []
    adjustments = []
    handled = []
    this_week = []
    last_week = []

    if int(dead.get("open_count") or 0):
        risks.append({"category": "automation", "title": "Open assistant dead letters", "reason": f"{dead.get('open_count')} open recovery item(s)"})
    for job in scheduler.get("problem_jobs") or []:
        risks.append({"category": "scheduler", "title": job, "reason": "scheduler state is not clean"})
    if int(hygiene.get("issue_count") or 0):
        risks.append({"category": "calendar_hygiene", "title": "Calendar hygiene needs review", "reason": f"{hygiene.get('issue_count')} issue(s) found"})
    for day in hygiene.get("overbooked_days") or []:
        risks.append({"category": "overload", "title": day.get("date"), "reason": "too little free time after noon"})
    for day in hygiene.get("no_realistic_focus_days") or []:
        risks.append({"category": "focus_space", "title": day.get("date"), "reason": "no 60-minute focus window"})

    high_tasks = [task for task in tasks.get("top_tasks") or [] if str(task.get("priority")) in {"Urgent", "High"}]
    for task in high_tasks[:6]:
        item = {"category": "task", "title": task.get("title"), "reason": f"{task.get('priority')} priority", "source_ref": task.get("source_ref"), "task_ref": task.get("page_id")}
        do_first.append(item)
        open_loops.append(item)
    if not high_tasks and int(tasks.get("open_count") or 0):
        open_loops.append({"category": "task", "title": f"{tasks.get('open_count')} open Rocky task(s)", "reason": "review for stale or low-priority backlog"})

    for item in coding.get("top_items") or []:
        if len(this_week) >= 4:
            break
        this_week.append({"category": "coding", "title": item.get("project") or item.get("title"), "reason": item.get("recommended_next_step") or "continue high-confidence coding work"})

    train = signals.get("training") or {}
    if train.get("target_date"):
        protect.append({"category": "training", "title": train.get("target_date"), "reason": train.get("status") or "training scheduler state"})
    email = signals.get("email") or {}
    if email.get("status") in {"created", "skipped_duplicate", "ok"}:
        handled.append("Email triage lane is operating or already protected.")
    if train.get("status") in {"created", "skipped_duplicate", "ok", "source_ref_drift_verified"}:
        handled.append("Training calendar lane is operating or already protected.")
    if int(learning.get("active_bounded_count") or 0):
        handled.append(f"{learning.get('active_bounded_count')} bounded learning preference(s) active.")
    elif int(learning.get("outcome_count") or 0):
        adjustments.append({"category": "learning", "title": "Learning calibration pending", "reason": "Rocky is observing outcomes but has not activated bounded preferences yet."})

    free_days = [day for day in calendar.get("days") or [] if int(day.get("max_focus_window_minutes") or 0) >= 90]
    if free_days:
        protect.append({"category": "deep_work", "title": ", ".join(str(day.get("date")) for day in free_days[:3]), "reason": "90+ minute focus windows exist"})
    if not do_first and this_week:
        do_first.append(this_week[0])
    if not do_first:
        do_first.append({"category": "calendar", "title": "Follow existing calendar and daily brief", "reason": "no stronger weekly signal"})

    last_week.append({"category": "assistant", "title": "Assistant lanes summarized", "reason": f"{len((signals.get('scheduler') or {}).get('states') or {})} scheduler state file(s) checked"})
    if int((signals.get("command_activity") or {}).get("command_count") or 0):
        handled.append(f"{(signals.get('command_activity') or {}).get('command_count')} recent task command(s) visible.")

    payload = {
        "status": "ok" if signals.get("status") == "ok" else "degraded",
        "reason": "weekly_priorities_planned",
        "policy_version": POLICY_VERSION,
        "week_label": signals.get("week_label"),
        "last_week": last_week,
        "this_week": this_week[:6],
        "protect": protect[:6],
        "do_first": do_first[:6],
        "risks_or_overloaded_days": risks[:8],
        "open_loops": open_loops[:8],
        "calendar_hygiene": {"status": hygiene.get("status"), "issue_count": hygiene.get("issue_count"), "summary": _hygiene_summary(hygiene)},
        "what_rocky_handled": handled[:8],
        "learning_or_calibration": {"status": learning.get("status"), "active_bounded_count": learning.get("active_bounded_count", 0), "proposal_count": learning.get("proposal_count", 0), "outcome_count": learning.get("outcome_count", 0)},
        "recommended_adjustments": adjustments[:6],
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    return _redact_payload(payload)


def _hygiene_summary(hygiene: dict[str, Any]) -> str:
    parts = []
    for key, label in [("duplicate_count", "duplicates"), ("stale_count", "stale state"), ("orphan_count", "orphans"), ("weekend_violation_count", "weekend violations"), ("overbooked_day_count", "overloaded days")]:
        if int(hygiene.get(key) or 0):
            parts.append(f"{hygiene.get(key)} {label}")
    return "; ".join(parts) if parts else "clean"


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
