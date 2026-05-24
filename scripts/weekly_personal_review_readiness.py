#!/usr/bin/env python3
"""Read-only readiness gate for Rocky's weekly personal review."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import redact_payload
from assistant_scheduler_health import WEEKLY_PERSONAL_REVIEW_SPEC, SchedulerJobSpec, evaluate_scheduler_job
from assistant_scheduler_state import DEFAULT_SCHEDULER_DB_PATH

READY_VERIFIED = "ready_verified"
READY_PENDING = "ready_pending_natural_run"
NOT_READY = "not_ready"
MANUAL_REVIEW = "manual_review_required"
JOB_NAME = "weekly_personal_review"
RUN_PREFIX = "weekly-personal-review:"


def evaluate_weekly_personal_review_readiness(*, expected_week: str | None = None, now_local: str | datetime | None = None, scheduler_db_path: str | Path | None = None, audit_log_path: str | Path | None = None, spec: SchedulerJobSpec = WEEKLY_PERSONAL_REVIEW_SPEC, health_payload: dict[str, Any] | None = None, recent_runs: list[dict[str, Any]] | None = None, dead_letters: list[dict[str, Any]] | None = None, launchctl_text: str | None = None, read_launchctl: bool = True) -> dict[str, Any]:
    tz = ZoneInfo(spec.launchagent.timezone)
    now = _parse_datetime(now_local, tz=tz) if now_local else datetime.now(tz)
    week_label = expected_week or _iso_week_label(now.date())
    monday = _monday_for_week(week_label)
    expected_run = datetime.combine(monday, time(spec.launchagent.hour, spec.launchagent.minute), tzinfo=tz)
    grace_until = expected_run + timedelta(minutes=spec.missing_log_grace_minutes)
    health = health_payload or evaluate_scheduler_job(spec, now=now, state_db_path=scheduler_db_path, audit_log_path=audit_log_path, write_state=False, write_audit=False, launchctl_text=launchctl_text, read_launchctl=read_launchctl)
    runs = recent_runs if recent_runs is not None else _read_recent_weekly_runs(scheduler_db_path, limit=10)
    open_dead = dead_letters if dead_letters is not None else _read_open_weekly_dead_letters(scheduler_db_path, limit=20)
    signals = health.get("signals") or {}; launchagent = signals.get("launchagent") or {}; launchctl = launchagent.get("launchctl") or {}; logs = signals.get("logs") or {}; helper_state = (signals.get("helper_state") or {}).get("state") or {}
    latest = _latest_run_for_week(runs, week_label)
    evidence = {"expected_run": expected_run.isoformat(), "grace_until": grace_until.isoformat(), "checked_at": now.isoformat(), "launchagent": {"label": launchagent.get("label") or spec.launchagent.label, "status": launchagent.get("status"), "loaded": launchctl.get("loaded"), "runs": launchctl.get("runs"), "last_exit_code": launchctl.get("last_exit_code"), "state": launchctl.get("state")}, "logs": {"stdout_path": logs.get("stdout_path"), "stderr_path": logs.get("stderr_path"), "stderr_size": logs.get("stderr_size"), "stderr_hash": logs.get("stderr_hash"), "status": logs.get("status")}, "scheduler_state": {"last_run_at": helper_state.get("last_run_at"), "last_status": helper_state.get("last_status"), "target_week": helper_state.get("target_week"), "target_date": helper_state.get("target_date"), "notification_status": helper_state.get("notification_status"), "calendar_write_attempted": helper_state.get("calendar_write_attempted"), "notion_write_attempted": helper_state.get("notion_write_attempted"), "error_hash": helper_state.get("error_hash")}, "recent_run": latest, "dead_letters": {"open_count": len(open_dead), "items": [_safe_dead_letter(item) for item in open_dead[:5]]}, "health_status": health.get("status"), "health_failure_class": health.get("failure_class")}
    status, reason, hints = _decide(now=now, grace_until=grace_until, week_label=week_label, health=health, launchagent=launchagent, logs=logs, helper_state=helper_state, latest_run=latest, open_dead_letters=open_dead)
    return redact_payload({"status": status, "reason": reason, "summary": _summary(status, week_label, reason), "expected_week": week_label, "expected_run": expected_run.isoformat(), "grace_until": grace_until.isoformat(), "production_ready": status == READY_VERIFIED, "recovery_hints": hints, "evidence": evidence, "calendar_write_attempted": False, "notion_write_attempted": False, "notification_sent": False})


def _decide(*, now: datetime, grace_until: datetime, week_label: str, health: dict[str, Any], launchagent: dict[str, Any], logs: dict[str, Any], helper_state: dict[str, Any], latest_run: dict[str, Any] | None, open_dead_letters: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    launchctl = launchagent.get("launchctl") or {}
    if launchagent.get("status") == "blocked" or not launchctl.get("loaded", True):
        return NOT_READY, "weekly_review_launchagent_not_loaded", ["Inspect and reload com.openclaw.rocky-weekly-personal-review before waiting for the next run."]
    if health.get("status") == "blocked":
        return NOT_READY, str(health.get("failure_class") or "weekly_review_health_blocked"), ["Run assistant-scheduler-health --job weekly_personal_review --json and fix the blocked health signal."]
    if int(logs.get("stderr_size") or 0) > 0:
        return NOT_READY, "weekly_review_stderr_not_empty", ["Inspect /Users/clawdbot/.openclaw/logs/rocky-weekly-personal-review.err.log before rerunning."]
    if now < grace_until:
        return READY_PENDING, "weekly_review_waiting_for_first_monday_natural_run", [f"Wait until after {grace_until.isoformat()} and rerun weekly-personal-review-readiness."]
    if launchctl.get("last_exit_code") not in {0, "0", None}:
        return NOT_READY, "weekly_review_launchagent_nonzero_exit", ["Inspect launchctl print output and weekly review stdout/stderr logs."]
    if str(helper_state.get("target_week") or "") != week_label:
        return NOT_READY, "weekly_review_state_target_week_mismatch", ["Inspect /Users/clawdbot/.openclaw/state/weekly_personal_review_scheduler.json for stale target_week."]
    if not latest_run:
        return NOT_READY, "weekly_review_recent_run_missing", ["Run weekly-personal-review-recent --limit 5 --json and inspect assistant_job_runs for the expected week."]
    if latest_run.get("status") not in {"ok", "degraded"}:
        return NOT_READY, "weekly_review_recent_run_not_successful", ["Inspect the latest weekly review run row before trusting production readiness."]
    if helper_state.get("calendar_write_attempted") or helper_state.get("notion_write_attempted"):
        return MANUAL_REVIEW, "weekly_review_unexpected_side_effect", ["Weekly review must remain read-mostly; inspect state and audit for unexpected Calendar or Notion writes."]
    if open_dead_letters:
        return MANUAL_REVIEW, "weekly_review_open_dead_letters", ["Inspect assistant-dead-letters --json for weekly_personal_review before accepting readiness."]
    if helper_state.get("notification_status") == "failed":
        return MANUAL_REVIEW, "weekly_review_notification_failed", ["Weekly review ran but Discord delivery failed; inspect dead letters and notification logs."]
    if helper_state.get("notification_status") != "posted":
        return NOT_READY, "weekly_review_notification_not_posted", ["The natural production run must post Discord, not dry-run or skip, before readiness is verified."]
    if helper_state.get("error_hash"):
        return MANUAL_REVIEW, "weekly_review_error_hash_present", ["Inspect the safe scheduler state error_hash and recent audit/dead-letter records."]
    return READY_VERIFIED, "weekly_review_natural_run_production_ready", []


def _read_recent_weekly_runs(db_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM assistant_job_runs WHERE job_name = ? AND idempotency_key LIKE ? ORDER BY created_at DESC LIMIT ?", (JOB_NAME, f"{RUN_PREFIX}%", max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _read_open_weekly_dead_letters(db_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM assistant_dead_letters WHERE job_name = ? AND status = 'open' ORDER BY updated_at DESC LIMIT ?", (JOB_NAME, max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _latest_run_for_week(runs: list[dict[str, Any]], week_label: str) -> dict[str, Any] | None:
    prefix = f"{RUN_PREFIX}{week_label}"
    for run in runs:
        if str(run.get("idempotency_key") or "").startswith(prefix):
            return _safe_run(run)
    return None


def _safe_run(run: dict[str, Any]) -> dict[str, Any]:
    summary = _parse_summary(run.get("summary"))
    return {"run_id": run.get("run_id"), "job_name": run.get("job_name"), "status": run.get("status"), "idempotency_key": run.get("idempotency_key"), "target_week": summary.get("target_week"), "notification_status": summary.get("notification_status"), "calendar_write_attempted": summary.get("calendar_write_attempted"), "notion_write_attempted": summary.get("notion_write_attempted"), "created_at": run.get("created_at"), "updated_at": run.get("updated_at"), "failure_class": run.get("failure_class"), "error_hash": run.get("error_hash")}


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["dead_letter_id", "job_name", "failure_class", "safe_summary", "recovery_hint", "attempts", "last_failed_at", "error_hash"]}


def _parse_summary(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}")); return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_datetime(value: str | datetime, *, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(tz)


def _monday_for_week(label: str) -> date:
    year, week = label.split("-W", 1)
    return date.fromisocalendar(int(year), int(week), 1)


def _iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar(); return f"{year}-W{week:02d}"


def _summary(status: str, week: str, reason: str) -> str:
    if status == READY_VERIFIED:
        return f"Weekly personal review is production-ready for {week}."
    if status == READY_PENDING:
        return f"Weekly personal review is still waiting for the natural Monday run for {week}."
    if status == MANUAL_REVIEW:
        return f"Weekly personal review ran but needs manual review for {week}: {reason}."
    return f"Weekly personal review is not ready for {week}: {reason}."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only readiness gate for Rocky weekly personal review.")
    parser.add_argument("--expected-week", dest="expected_week")
    parser.add_argument("--now-local", dest="now_local")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--audit-ledger", dest="audit_ledger")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = evaluate_weekly_personal_review_readiness(expected_week=args.expected_week, now_local=args.now_local, scheduler_db_path=args.state_db, audit_log_path=args.audit_ledger)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else payload.get("summary"))
    return 0 if payload.get("status") == READY_VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())
