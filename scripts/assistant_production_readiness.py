#!/usr/bin/env python3
"""Unified read-only production readiness rollup for Rocky assistant lanes."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agentmail_bridge_health import build_agentmail_bridge_health
from assistant_audit_log import redact_payload
from assistant_calendar_status import calendar_write_health
from assistant_learning_readiness import evaluate_assistant_learning_readiness
from assistant_scheduler_health import evaluate_all_scheduler_jobs
from assistant_scheduler_state import DEFAULT_SCHEDULER_DB_PATH, AssistantSchedulerState
from daily_personal_briefing_readiness import evaluate_daily_personal_briefing_readiness
from notion_task_manager import notion_task_health
from weekly_calendar_hygiene import inspect_weekly_calendar_hygiene
from weekly_personal_review_readiness import evaluate_weekly_personal_review_readiness

READY_VERIFIED = "ready_verified"
READY_PENDING = "ready_pending_natural_runs"
MANUAL_REVIEW = "manual_review_required"
NOT_READY = "not_ready"
TIMEZONE = "Europe/Prague"

CRITICAL_JOB_NAMES = {
    "betty_mail_triage",
    "training_calendar_booking",
    "email_triage_booking",
    "task_spine",
    "coding_work_briefing",
    "task_command_capture",
    "daily_personal_briefing",
    "assistant_learning",
    "weekly_personal_review",
}
ACCEPTABLE_LEARNING_STATUSES = {"ready_verified", "calibration_pending"}
PENDING_GATE_STATUS = "ready_pending_natural_run"


def build_assistant_production_readiness(
    *,
    expected_date: str | date | None = None,
    expected_week: str | None = None,
    now_local: str | datetime | None = None,
    scheduler_db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    learning_db_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    calendar_db_path: str | Path | None = None,
    scheduler_health_payload: dict[str, Any] | None = None,
    daily_readiness_payload: dict[str, Any] | None = None,
    weekly_readiness_payload: dict[str, Any] | None = None,
    learning_readiness_payload: dict[str, Any] | None = None,
    calendar_write_health_payload: dict[str, Any] | None = None,
    calendar_hygiene_payload: dict[str, Any] | None = None,
    dead_letters: list[dict[str, Any]] | None = None,
    agentmail_health_payload: dict[str, Any] | None = None,
    notion_health_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tz = ZoneInfo(TIMEZONE)
    now = _parse_datetime(now_local, tz=tz) if now_local else datetime.now(tz)
    target_date = _parse_date(expected_date) if expected_date else now.date()
    week_label = expected_week or _iso_week_label(target_date)
    scheduler_db = Path(scheduler_db_path) if scheduler_db_path else DEFAULT_SCHEDULER_DB_PATH

    scheduler_health = scheduler_health_payload or evaluate_all_scheduler_jobs(
        state_db_path=scheduler_db,
        audit_log_path=audit_log_path,
        write_state=False,
        write_audit=False,
    )
    daily = daily_readiness_payload or evaluate_daily_personal_briefing_readiness(
        expected_date=target_date,
        now_local=now,
        scheduler_db_path=scheduler_db,
        audit_log_path=audit_log_path,
    )
    weekly = weekly_readiness_payload or evaluate_weekly_personal_review_readiness(
        expected_week=week_label,
        now_local=now,
        scheduler_db_path=scheduler_db,
        audit_log_path=audit_log_path,
    )
    learning = learning_readiness_payload or evaluate_assistant_learning_readiness(
        expected_date=target_date,
        now_local=now,
        scheduler_db_path=scheduler_db,
        learning_db_path=learning_db_path,
        audit_log_path=audit_log_path,
    )
    cal_health = calendar_write_health_payload or calendar_write_health(
        db_path=calendar_db_path,
        ledger_path=audit_log_path,
        write_audit=False,
    )
    hygiene = calendar_hygiene_payload or inspect_weekly_calendar_hygiene(
        start_date=target_date,
        days=14,
        state_db_path=calendar_state_db_path,
        db_path=calendar_db_path,
        ledger_path=audit_log_path,
    )
    open_dead_letters = dead_letters if dead_letters is not None else _read_open_dead_letters(scheduler_db)
    agentmail = agentmail_health_payload or build_agentmail_bridge_health(run_tests=False, read_launchctl=True)
    notion = notion_health_payload or notion_task_health()

    not_ready_items: list[dict[str, Any]] = []
    manual_review_items: list[dict[str, Any]] = []
    pending_gates: list[str] = []

    _classify_scheduler_health(scheduler_health, not_ready_items, manual_review_items)
    _classify_gate("daily_personal_briefing", daily, pending_gates, not_ready_items, manual_review_items)
    _classify_gate("weekly_personal_review", weekly, pending_gates, not_ready_items, manual_review_items)
    _classify_gate("assistant_learning", learning, pending_gates, not_ready_items, manual_review_items, acceptable=ACCEPTABLE_LEARNING_STATUSES)
    _classify_calendar_health(cal_health, not_ready_items)
    _classify_calendar_hygiene(hygiene, manual_review_items, not_ready_items)
    _classify_dead_letters(open_dead_letters, manual_review_items)
    _classify_optional_health("agentmail_bridge", agentmail, not_ready_items, manual_review_items)
    _classify_optional_health("notion_task_health", notion, not_ready_items, manual_review_items)

    if not_ready_items:
        status = NOT_READY
    elif pending_gates:
        status = READY_PENDING
    elif manual_review_items:
        status = MANUAL_REVIEW
    else:
        status = READY_VERIFIED

    payload = {
        "status": status,
        "production_ready": status == READY_VERIFIED,
        "summary": _summary(status, not_ready_items=not_ready_items, manual_review_items=manual_review_items, pending_gates=pending_gates),
        "checked_at": now.isoformat(),
        "expected_date": target_date.isoformat(),
        "expected_week": week_label,
        "pending_gates": pending_gates,
        "not_ready_items": not_ready_items,
        "manual_review_items": manual_review_items,
        "lanes": {
            "scheduler_health": _safe_scheduler_summary(scheduler_health),
            "daily_personal_briefing": _safe_gate(daily),
            "weekly_personal_review": _safe_gate(weekly),
            "assistant_learning": _safe_gate(learning),
            "calendar_write_health": _safe_calendar_health(cal_health),
            "calendar_hygiene": _safe_hygiene(hygiene),
            "dead_letters": {"open_count": len(open_dead_letters), "items": [_safe_dead_letter(item) for item in open_dead_letters[:10]]},
            "agentmail_bridge": _safe_health(agentmail),
            "notion_task_health": _safe_health(notion),
        },
        "safe_recovery": _safe_recovery_summary(hygiene, open_dead_letters),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    }
    return redact_payload(payload)


def _classify_scheduler_health(payload: dict[str, Any], not_ready: list[dict[str, Any]], manual: list[dict[str, Any]]) -> None:
    for job in payload.get("jobs") or []:
        name = str(job.get("job_name") or "")
        if name not in CRITICAL_JOB_NAMES:
            continue
        status = str(job.get("status") or "unknown")
        if status == "blocked":
            not_ready.append({"source": "scheduler_health", "job_name": name, "status": status, "reason": job.get("failure_class") or "scheduler_blocked"})
        elif status in {"degraded", "unknown"}:
            manual.append({"source": "scheduler_health", "job_name": name, "status": status, "reason": job.get("failure_class") or "scheduler_degraded"})


def _classify_gate(name: str, payload: dict[str, Any], pending: list[str], not_ready: list[dict[str, Any]], manual: list[dict[str, Any]], *, acceptable: set[str] | None = None) -> None:
    acceptable = acceptable or {READY_VERIFIED}
    status = str(payload.get("status") or "")
    if status in acceptable:
        return
    if status == PENDING_GATE_STATUS:
        pending.append(name)
    elif status == MANUAL_REVIEW:
        manual.append({"source": name, "status": status, "reason": payload.get("reason")})
    else:
        not_ready.append({"source": name, "status": status, "reason": payload.get("reason") or "readiness_gate_not_ready"})


def _classify_calendar_health(payload: dict[str, Any], not_ready: list[dict[str, Any]]) -> None:
    if payload.get("status") != "ok":
        not_ready.append({"source": "calendar_write_health", "status": payload.get("status"), "reason": "calendar_write_health_blocked", "blocked_checks": payload.get("blocked_checks") or []})


def _classify_calendar_hygiene(payload: dict[str, Any], manual: list[dict[str, Any]], not_ready: list[dict[str, Any]]) -> None:
    status = str(payload.get("status") or "")
    if status == "degraded":
        not_ready.append({"source": "calendar_hygiene", "status": status, "reason": payload.get("reason")})
    elif status == "manual_review_required" or int(payload.get("issue_count") or 0) > 0:
        manual.append({
            "source": "calendar_hygiene",
            "status": status,
            "reason": payload.get("reason") or "calendar_hygiene_issues_found",
            "issue_count": payload.get("issue_count"),
            "orphan_count": len(payload.get("orphan_rocky_events") or []),
            "stale_count": len(payload.get("stale_state_candidates") or []),
        })


def _classify_dead_letters(items: list[dict[str, Any]], manual: list[dict[str, Any]]) -> None:
    if items:
        manual.append({"source": "assistant_dead_letters", "status": "manual_review_required", "open_count": len(items), "reason": "open_dead_letters_present"})


def _classify_optional_health(name: str, payload: dict[str, Any], not_ready: list[dict[str, Any]], manual: list[dict[str, Any]]) -> None:
    status = str(payload.get("status") or "unknown")
    if status == "blocked":
        not_ready.append({"source": name, "status": status, "reason": payload.get("reason") or payload.get("recommendation") or "blocked"})
    elif status not in {"ok", "healthy"}:
        manual.append({"source": name, "status": status, "reason": payload.get("reason") or payload.get("recommendation") or "degraded"})


def _read_open_dead_letters(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        state = AssistantSchedulerState(path)
        return state.list_dead_letters(status="open", limit=100)
    except (OSError, sqlite3.Error):
        return []


def _safe_scheduler_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "job_count": len(payload.get("jobs") or []),
        "jobs": [
            {"job_name": item.get("job_name"), "status": item.get("status"), "failure_class": item.get("failure_class")}
            for item in (payload.get("jobs") or [])
        ],
    }


def _safe_gate(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ["status", "reason", "summary", "expected_date", "expected_week", "expected_run", "grace_until", "production_ready", "bounded_preferences_active"]}


def _safe_calendar_health(payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": payload.get("status"), "blocked_checks": payload.get("blocked_checks") or [], "calendar_write_attempted": bool(payload.get("calendar_write_attempted"))}


def _safe_hygiene(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "issue_count": payload.get("issue_count", 0),
        "orphan_rocky_events": payload.get("orphan_rocky_events") or [],
        "stale_state_candidates": payload.get("stale_state_candidates") or [],
        "duplicate_rocky_blocks": payload.get("duplicate_rocky_blocks") or [],
        "weekend_policy_violations": payload.get("weekend_policy_violations") or [],
        "calendar_write_attempted": bool(payload.get("calendar_write_attempted")),
        "notion_write_attempted": bool(payload.get("notion_write_attempted")),
    }


def _safe_health(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "recommendation": payload.get("recommendation"),
        "service": payload.get("service"),
        "access_configured": bool(payload.get("token_configured")) if "token_configured" in payload else None,
        "database_configured": payload.get("database_configured"),
        "parent_configured": payload.get("parent_configured"),
    }


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["dead_letter_id", "job_name", "failure_class", "safe_summary", "recovery_hint", "attempts", "last_failed_at", "error_hash"]}


def _safe_recovery_summary(hygiene: dict[str, Any], dead_letters: list[dict[str, Any]]) -> dict[str, Any]:
    stale = hygiene.get("stale_state_candidates") or []
    orphan = hygiene.get("orphan_rocky_events") or []
    return {
        "candidate_count": len(stale) + len(orphan) + len(dead_letters),
        "state_only_candidate_count": len(stale) + len(dead_letters),
        "manual_only_candidate_count": len(orphan),
        "command": "assistant-safe-recovery --json",
    }


def _summary(status: str, *, not_ready_items: list[dict[str, Any]], manual_review_items: list[dict[str, Any]], pending_gates: list[str]) -> str:
    if status == READY_VERIFIED:
        return "Rocky production readiness is verified across assistant lanes."
    if status == READY_PENDING:
        suffix = f" Manual-review evidence is also present: {len(manual_review_items)} item(s)." if manual_review_items else ""
        return f"Rocky is healthy but waiting on natural-run verification for: {', '.join(pending_gates)}.{suffix}"
    if status == MANUAL_REVIEW:
        return f"Rocky needs manual review for {len(manual_review_items)} production-readiness item(s)."
    return f"Rocky is not production-ready; {len(not_ready_items)} blocking item(s) were found."


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_datetime(value: str | datetime, *, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(tz)


def _iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only production readiness rollup for Rocky.")
    parser.add_argument("--expected-date", dest="expected_date")
    parser.add_argument("--expected-week", dest="expected_week")
    parser.add_argument("--now-local", dest="now_local")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--learning-db", dest="learning_db")
    parser.add_argument("--audit-ledger", dest="audit_ledger")
    parser.add_argument("--calendar-db-path", dest="calendar_db_path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_assistant_production_readiness(
        expected_date=args.expected_date,
        expected_week=args.expected_week,
        now_local=args.now_local,
        scheduler_db_path=args.state_db,
        calendar_state_db_path=args.calendar_state_db,
        learning_db_path=args.learning_db,
        audit_log_path=args.audit_ledger,
        calendar_db_path=args.calendar_db_path,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else payload.get("summary"))
    return 0 if payload.get("status") in {READY_VERIFIED, READY_PENDING, MANUAL_REVIEW} else 1


if __name__ == "__main__":
    raise SystemExit(main())
