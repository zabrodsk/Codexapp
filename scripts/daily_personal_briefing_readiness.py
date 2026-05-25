#!/usr/bin/env python3
"""Read-only production readiness gate for Rocky's daily briefing natural run."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from assistant_audit_log import DEFAULT_AUDIT_LEDGER_PATH, redact_payload
from assistant_scheduler_health import DAILY_PERSONAL_BRIEFING_SPEC, SchedulerJobSpec, evaluate_scheduler_job
from assistant_scheduler_state import DEFAULT_SCHEDULER_DB_PATH


READY_VERIFIED = "ready_verified"
READY_PENDING = "ready_pending_natural_run"
NOT_READY = "not_ready"
MANUAL_REVIEW = "manual_review_required"

DAILY_JOB_NAME = "daily_personal_briefing"
DAILY_RUN_KEY_PREFIX = "daily-personal-briefing:"
ALLOWED_BOOKING_ACTIONS = {"email_triage_repair", "coding_focus_book", "task_focus_book"}
ALLOWED_BOOKING_STATUSES = {
    "created",
    "skipped_duplicate",
    "ok",
    "skipped_no_attention_emails",
    "skipped_no_coding_focus",
    "skipped_no_focus_tasks",
}


def evaluate_daily_personal_briefing_readiness(
    *,
    expected_date: str | date | None = None,
    now_local: str | datetime | None = None,
    scheduler_db_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    spec: SchedulerJobSpec = DAILY_PERSONAL_BRIEFING_SPEC,
    health_payload: dict[str, Any] | None = None,
    recent_payload: dict[str, Any] | None = None,
    dead_letters: list[dict[str, Any]] | None = None,
    audit_events: list[dict[str, Any]] | None = None,
    launchctl_text: str | None = None,
    read_launchctl: bool = True,
) -> dict[str, Any]:
    """Return the production readiness decision for the first weekday run.

    This function is intentionally read-only. It reuses scheduler health and
    state stores but does not write audit, health, job-run, or dead-letter rows.
    """

    tz = ZoneInfo(spec.launchagent.timezone)
    now = _parse_datetime(now_local, tz=tz) if now_local else datetime.now(tz)
    target_date = _parse_date(expected_date) if expected_date else _default_expected_date(spec)
    expected_run = datetime.combine(target_date, time(spec.launchagent.hour or 0, spec.launchagent.minute or 0), tzinfo=tz)
    grace_until = expected_run + timedelta(minutes=spec.missing_log_grace_minutes)

    health = health_payload or evaluate_scheduler_job(
        spec,
        now=now,
        state_db_path=scheduler_db_path,
        audit_log_path=audit_log_path,
        write_state=False,
        write_audit=False,
        launchctl_text=launchctl_text,
        read_launchctl=read_launchctl,
    )
    recent = recent_payload or _read_recent_daily_runs(scheduler_db_path, limit=10)
    relevant_dead_letters = dead_letters if dead_letters is not None else _read_daily_dead_letters(scheduler_db_path, limit=20)
    open_dead_letters = [item for item in relevant_dead_letters if str(item.get("status") or "open") in {"open", "notified", "waiting_for_user", "ack_failed"}]
    notification_compensated = any(
        str(item.get("failure_class") or "") == "daily_personal_briefing_notification_failed"
        and str(item.get("status") or "") == "recovered"
        for item in relevant_dead_letters
    )
    audit = audit_events if audit_events is not None else _read_recent_daily_audit(audit_log_path, limit=10)

    signals = health.get("signals") or {}
    natural = signals.get("natural_run") or {}
    launchagent = signals.get("launchagent") or {}
    launchctl = launchagent.get("launchctl") or {}
    logs = signals.get("logs") or {}
    helper_state = (signals.get("helper_state") or {}).get("state") or {}
    latest_run = _latest_run_for_date(recent.get("runs") or [], target_date.isoformat())
    notification_status = str(helper_state.get("notification_status") or (latest_run or {}).get("notification_status") or "")
    calendar_summary = _calendar_side_effect_summary(helper_state, latest_run)

    evidence = {
        "expected_run": expected_run.isoformat(),
        "grace_until": grace_until.isoformat(),
        "checked_at": now.isoformat(),
        "launchagent": {
            "label": launchagent.get("label") or spec.launchagent.label,
            "status": launchagent.get("status"),
            "loaded": launchctl.get("loaded"),
            "runs": launchctl.get("runs"),
            "last_exit_code": launchctl.get("last_exit_code"),
            "state": launchctl.get("state"),
        },
        "logs": {
            "stdout_path": logs.get("stdout_path"),
            "stderr_path": logs.get("stderr_path"),
            "stderr_size": logs.get("stderr_size"),
            "stderr_hash": logs.get("stderr_hash"),
            "status": logs.get("status"),
        },
        "scheduler_state": {
            "last_run_at": helper_state.get("last_run_at"),
            "last_status": helper_state.get("last_status"),
            "target_date": helper_state.get("target_date"),
            "reason": helper_state.get("reason"),
            "notification_status": notification_status or None,
            "created_count": helper_state.get("created_count", 0),
            "skipped_count": helper_state.get("skipped_count", 0),
            "error_hash": helper_state.get("error_hash"),
        },
        "recent_run": latest_run,
        "dead_letters": {
            "open_count": len(open_dead_letters),
            "items": [_safe_dead_letter(item) for item in open_dead_letters[:5]],
        },
        "audit": _audit_summary(audit),
        "calendar_side_effects": calendar_summary,
        "health_status": health.get("status"),
        "health_failure_class": health.get("failure_class"),
        "natural_run_status": natural.get("status"),
    }

    status, reason, hints = _decide_readiness(
        expected_date=target_date,
        grace_until=grace_until,
        now=now,
        health=health,
        natural=natural,
        launchagent=launchagent,
        logs=logs,
        helper_state=helper_state,
        latest_run=latest_run,
        notification_status=notification_status,
        open_dead_letters=open_dead_letters,
        notification_compensated=notification_compensated,
        calendar_summary=calendar_summary,
    )

    summary = _human_summary(status=status, expected_date=target_date, reason=reason)
    payload = {
        "status": status,
        "reason": reason,
        "summary": summary,
        "expected_date": target_date.isoformat(),
        "expected_run": expected_run.isoformat(),
        "grace_until": grace_until.isoformat(),
        "production_ready": status == READY_VERIFIED,
        "recovery_hints": hints,
        "evidence": evidence,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    }
    return redact_payload(payload)


def _decide_readiness(
    *,
    expected_date: date,
    grace_until: datetime,
    now: datetime,
    health: dict[str, Any],
    natural: dict[str, Any],
    launchagent: dict[str, Any],
    logs: dict[str, Any],
    helper_state: dict[str, Any],
    latest_run: dict[str, Any] | None,
    notification_status: str,
    open_dead_letters: list[dict[str, Any]],
    notification_compensated: bool,
    calendar_summary: dict[str, Any],
) -> tuple[str, str, list[str]]:
    launchctl = launchagent.get("launchctl") or {}
    if launchagent.get("status") == "blocked" or not launchctl.get("loaded", True):
        return NOT_READY, "daily_briefing_launchagent_not_loaded", ["Inspect and reload com.openclaw.rocky-daily-personal-briefing before waiting for the next run."]
    if health.get("status") == "blocked":
        return NOT_READY, str(health.get("failure_class") or "daily_briefing_health_blocked"), ["Run assistant-scheduler-health --job daily_personal_briefing --json and fix the blocked health signal."]
    if int(logs.get("stderr_size") or 0) > 0:
        return NOT_READY, "daily_briefing_stderr_not_empty", ["Inspect /Users/clawdbot/.openclaw/logs/rocky-daily-personal-briefing.err.log before rerunning."]
    if launchctl.get("last_exit_code") not in {0, "0", None} and now >= grace_until:
        return NOT_READY, "daily_briefing_launchagent_nonzero_exit", ["Inspect launchctl print output and stdout/stderr logs for the daily briefing LaunchAgent."]
    if now < grace_until or natural.get("status") == "pending_first_weekday_run":
        return READY_PENDING, "daily_briefing_waiting_for_first_weekday_natural_run", [f"Wait until after {grace_until.isoformat()} and rerun daily-personal-briefing-readiness."]
    if natural.get("status") != "natural_run_verified":
        return NOT_READY, "daily_briefing_natural_run_not_verified", ["Inspect assistant-scheduler-health natural_run details, LaunchAgent run count, scheduler state, and logs."]
    if str(helper_state.get("target_date") or "") != expected_date.isoformat():
        return NOT_READY, "daily_briefing_state_target_date_mismatch", ["Inspect /Users/clawdbot/.openclaw/state/daily_personal_briefing_scheduler.json for stale target_date."]
    if not latest_run:
        return NOT_READY, "daily_briefing_recent_run_missing", ["Run daily-personal-briefing-recent --limit 5 --json and inspect assistant_job_runs for the expected date."]
    if latest_run.get("status") not in {"ok", "degraded"}:
        return NOT_READY, "daily_briefing_recent_run_not_successful", ["Inspect the latest daily-personal-briefing-recent row and rerun only after understanding the failure."]
    if calendar_summary.get("unexpected_count"):
        return MANUAL_REVIEW, "daily_briefing_unexpected_calendar_side_effect", ["Inspect daily briefing booking_results and confirm any Calendar writes came through allowed lane rails before rerunning."]
    if calendar_summary.get("evidence_incomplete"):
        return MANUAL_REVIEW, "daily_briefing_calendar_side_effect_evidence_incomplete", ["Inspect scheduler state and audit rows; created_count is nonzero but booking_results are missing."]
    if notification_status == "failed":
        if notification_compensated:
            return READY_VERIFIED, "daily_briefing_notification_compensated_by_agentmail_fallback", []
        if open_dead_letters:
            return MANUAL_REVIEW, "daily_briefing_notification_failed_with_dead_letter", ["Inspect assistant-dead-letters --json; briefing work completed but Discord delivery needs attention."]
        return NOT_READY, "daily_briefing_notification_failed_without_dead_letter", ["Discord delivery failed without a safe dead letter; inspect scheduler output and notification dispatcher logs."]
    if notification_status != "posted":
        return NOT_READY, "daily_briefing_notification_not_posted", ["The natural production run must post Discord, not dry-run or skip, before readiness is verified."]
    if helper_state.get("error_hash"):
        return MANUAL_REVIEW, "daily_briefing_error_hash_present", ["Inspect the safe scheduler state error_hash and recent audit/dead-letter records."]
    return READY_VERIFIED, "daily_briefing_natural_run_production_ready", []


def _calendar_side_effect_summary(helper_state: dict[str, Any], latest_run: dict[str, Any] | None) -> dict[str, Any]:
    booking_results = helper_state.get("booking_results") if isinstance(helper_state.get("booking_results"), list) else []
    writes = [item for item in booking_results if bool(item.get("calendar_write_attempted"))]
    unexpected = [
        item for item in writes
        if str(item.get("action") or "") not in ALLOWED_BOOKING_ACTIONS
        or str(item.get("status") or "") not in ALLOWED_BOOKING_STATUSES
    ]
    created_count = int(helper_state.get("created_count") or (latest_run or {}).get("created_count") or 0)
    return {
        "calendar_write_attempted": bool(writes) or created_count > 0,
        "created_count": created_count,
        "skipped_count": int(helper_state.get("skipped_count") or (latest_run or {}).get("skipped_count") or 0),
        "safe_booking_mode": helper_state.get("safe_booking_mode") or (latest_run or {}).get("safe_booking_mode"),
        "booking_result_count": len(booking_results),
        "unexpected_count": len(unexpected),
        "unexpected": unexpected[:3],
        "evidence_incomplete": created_count > 0 and not booking_results,
    }


def _read_recent_daily_runs(db_path: str | Path | None, *, limit: int) -> dict[str, Any]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return {"status": "ok", "count": 0, "runs": [], "calendar_write_attempted": False, "notion_write_attempted": False}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM assistant_job_runs
                WHERE job_name = ? AND idempotency_key LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (DAILY_JOB_NAME, f"{DAILY_RUN_KEY_PREFIX}%", max(1, int(limit))),
            ).fetchall()
    except sqlite3.Error:
        return {"status": "degraded", "count": 0, "runs": [], "calendar_write_attempted": False, "notion_write_attempted": False}
    runs = []
    for row in rows:
        summary = _parse_summary(row["summary"])
        runs.append(
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "target_date": summary.get("target_date") or row["scheduled_for"],
                "reason": summary.get("reason") or row["failure_class"],
                "notification_status": summary.get("notification_status"),
                "safe_booking_mode": summary.get("safe_booking_mode"),
                "top_priority": summary.get("top_priority"),
                "created_count": summary.get("created_count", 0),
                "skipped_count": summary.get("skipped_count", 0),
                "message_sha256": summary.get("message_sha256"),
                "idempotency_key": row["idempotency_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return {"status": "ok", "count": len(runs), "runs": runs, "calendar_write_attempted": False, "notion_write_attempted": False}


def _read_daily_dead_letters(db_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM assistant_dead_letters
                WHERE job_name = ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (DAILY_JOB_NAME, max(1, int(limit))),
            ).fetchall()

    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _read_recent_daily_audit(ledger_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(ledger_path) if ledger_path else DEFAULT_AUDIT_LEDGER_PATH
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if str(event.get("workflow") or "").startswith("daily_personal_briefing"):
                events.append(event)
    except (OSError, json.JSONDecodeError):
        return []
    return events[-max(1, int(limit)):][::-1]


def _latest_run_for_date(runs: list[dict[str, Any]], expected_date: str) -> dict[str, Any] | None:
    for run in runs:
        if str(run.get("target_date") or "") == expected_date:
            return run
    return None


def _audit_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest = events[0] if events else None
    return {
        "recent_count": len(events),
        "latest": {
            "audit_id": latest.get("audit_id"),
            "event_type": latest.get("event_type"),
            "created_at": latest.get("created_at"),
            "decision": latest.get("decision"),
        } if latest else None,
    }


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "dead_letter_id": item.get("dead_letter_id"),
        "failure_class": item.get("failure_class"),
        "safe_summary": item.get("safe_summary"),
        "recovery_hint": item.get("recovery_hint"),
        "last_failed_at": item.get("last_failed_at"),
        "attempts": item.get("attempts"),
    }


def _human_summary(*, status: str, expected_date: date, reason: str) -> str:
    if status == READY_VERIFIED:
        return f"Daily briefing natural run for {expected_date.isoformat()} is production-ready."
    if status == READY_PENDING:
        return f"Daily briefing natural run for {expected_date.isoformat()} is still pending its scheduled grace window."
    if status == MANUAL_REVIEW:
        return f"Daily briefing natural run for {expected_date.isoformat()} needs manual review: {reason}."
    return f"Daily briefing natural run for {expected_date.isoformat()} is not production-ready: {reason}."


def _parse_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _parse_datetime(value: str | datetime, *, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _default_expected_date(spec: SchedulerJobSpec) -> date:
    value = spec.first_expected_run_after or spec.launchagent.first_expected_run_after
    if value:
        return _parse_datetime(value, tz=ZoneInfo(spec.launchagent.timezone)).date()
    return datetime.now(ZoneInfo(spec.launchagent.timezone)).date()


def _parse_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {"summary_hash": hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]}
