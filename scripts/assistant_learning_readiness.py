#!/usr/bin/env python3
"""Read-only production readiness gate for Rocky assistant learning."""
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

from assistant_audit_log import DEFAULT_AUDIT_LEDGER_PATH, redact_payload
from assistant_learning_store import DEFAULT_LEARNING_DB_PATH
from assistant_preference_models import learning_summary
from assistant_scheduler_health import ASSISTANT_LEARNING_SPEC, SchedulerJobSpec, evaluate_scheduler_job
from assistant_scheduler_state import DEFAULT_SCHEDULER_DB_PATH

READY_VERIFIED = "ready_verified"
READY_PENDING = "ready_pending_natural_run"
CALIBRATION_PENDING = "calibration_pending"
NOT_READY = "not_ready"
MANUAL_REVIEW = "manual_review_required"

JOB_NAME = "assistant_learning"
RUN_KEY_PREFIX = "assistant-learning:"


def evaluate_assistant_learning_readiness(
    *,
    expected_date: str | date | None = None,
    now_local: str | datetime | None = None,
    scheduler_db_path: str | Path | None = None,
    learning_db_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    spec: SchedulerJobSpec = ASSISTANT_LEARNING_SPEC,
    health_payload: dict[str, Any] | None = None,
    learning_summary_payload: dict[str, Any] | None = None,
    recent_runs: list[dict[str, Any]] | None = None,
    dead_letters: list[dict[str, Any]] | None = None,
    audit_events: list[dict[str, Any]] | None = None,
    launchctl_text: str | None = None,
    read_launchctl: bool = True,
) -> dict[str, Any]:
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
    summary = learning_summary_payload or learning_summary(db_path=learning_db_path)
    runs = recent_runs if recent_runs is not None else _read_recent_learning_runs(scheduler_db_path, limit=10)
    open_dead_letters = dead_letters if dead_letters is not None else _read_open_learning_dead_letters(scheduler_db_path, limit=20)
    audit = audit_events if audit_events is not None else _read_recent_learning_audit(audit_log_path, limit=10)

    signals = health.get("signals") or {}
    natural = signals.get("natural_run") or {}
    launchagent = signals.get("launchagent") or {}
    launchctl = launchagent.get("launchctl") or {}
    logs = signals.get("logs") or {}
    helper_state = (signals.get("helper_state") or {}).get("state") or {}
    latest_run = _latest_run_for_date(runs, target_date.isoformat())
    active_count = int(summary.get("active_bounded_count") or 0)
    proposal_count = int(summary.get("proposal_count") or 0)
    outcome_count = int(summary.get("outcome_count") or 0)

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
            "outcome_count": helper_state.get("outcome_count"),
            "active_bounded_count": helper_state.get("active_bounded_count"),
            "proposal_count": helper_state.get("proposal_count"),
            "error_hash": helper_state.get("error_hash"),
        },
        "recent_run": latest_run,
        "learning_summary": {
            "status": summary.get("status"),
            "active_bounded_count": active_count,
            "proposal_count": proposal_count,
            "outcome_count": outcome_count,
            "preferences": _safe_preferences(summary.get("preferences") or []),
            "proposals": _safe_proposals(summary.get("proposals") or []),
        },
        "dead_letters": {"open_count": len(open_dead_letters), "items": [_safe_dead_letter(item) for item in open_dead_letters[:5]]},
        "audit": _audit_summary(audit),
        "health_status": health.get("status"),
        "health_failure_class": health.get("failure_class"),
        "natural_run_status": natural.get("status"),
    }

    status, reason, hints = _decide_readiness(
        now=now,
        grace_until=grace_until,
        expected_date=target_date,
        health=health,
        natural=natural,
        launchagent=launchagent,
        logs=logs,
        helper_state=helper_state,
        latest_run=latest_run,
        summary=summary,
        open_dead_letters=open_dead_letters,
    )
    payload = {
        "status": status,
        "reason": reason,
        "summary": _human_summary(status=status, expected_date=target_date, reason=reason),
        "expected_date": target_date.isoformat(),
        "expected_run": expected_run.isoformat(),
        "grace_until": grace_until.isoformat(),
        "production_ready": status in {READY_VERIFIED, CALIBRATION_PENDING},
        "bounded_preferences_active": active_count > 0,
        "recovery_hints": hints,
        "evidence": evidence,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    }
    return redact_payload(payload)


def _decide_readiness(*, now: datetime, grace_until: datetime, expected_date: date, health: dict[str, Any], natural: dict[str, Any], launchagent: dict[str, Any], logs: dict[str, Any], helper_state: dict[str, Any], latest_run: dict[str, Any] | None, summary: dict[str, Any], open_dead_letters: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    launchctl = launchagent.get("launchctl") or {}
    if launchagent.get("status") == "blocked" or not launchctl.get("loaded", True):
        return NOT_READY, "assistant_learning_launchagent_not_loaded", ["Inspect and reload com.openclaw.rocky-assistant-learning before waiting for the next run."]
    if health.get("status") == "blocked":
        return NOT_READY, str(health.get("failure_class") or "assistant_learning_health_blocked"), ["Run assistant-scheduler-health --job assistant_learning --json and fix the blocked health signal."]
    if int(logs.get("stderr_size") or 0) > 0:
        return NOT_READY, "assistant_learning_stderr_not_empty", ["Inspect /Users/clawdbot/.openclaw/logs/rocky-assistant-learning.err.log before rerunning."]
    if launchctl.get("last_exit_code") not in {0, "0", None} and now >= grace_until:
        return NOT_READY, "assistant_learning_launchagent_nonzero_exit", ["Inspect launchctl print output and stdout/stderr logs for the assistant learning LaunchAgent."]
    if now < grace_until or natural.get("status") == "pending_first_weekday_run":
        return READY_PENDING, "assistant_learning_waiting_for_first_weekday_natural_run", [f"Wait until after {grace_until.isoformat()} and rerun assistant-learning-readiness."]
    if natural.get("status") != "natural_run_verified":
        return NOT_READY, "assistant_learning_natural_run_not_verified", ["Inspect assistant-scheduler-health natural_run details, LaunchAgent run count, scheduler state, and logs."]
    if str(helper_state.get("target_date") or "") != expected_date.isoformat():
        return NOT_READY, "assistant_learning_state_target_date_mismatch", ["Inspect /Users/clawdbot/.openclaw/state/assistant_learning_scheduler.json for stale target_date."]
    if summary.get("status") == "empty":
        return NOT_READY, "assistant_learning_store_missing", ["Run assistant-outcomes-collect --live and assistant-learning-scheduler-run --live after confirming the learning DB path."]
    if not latest_run:
        return NOT_READY, "assistant_learning_recent_run_missing", ["Inspect assistant_job_runs for an assistant-learning run on the expected date."]
    if latest_run.get("status") not in {"succeeded", "stale"}:
        return NOT_READY, "assistant_learning_recent_run_not_successful", ["Inspect the latest assistant learning job run row before trusting learned preferences."]
    if open_dead_letters:
        return MANUAL_REVIEW, "assistant_learning_open_dead_letters", ["Inspect assistant-dead-letters --json before accepting learning readiness."]
    if helper_state.get("error_hash"):
        return MANUAL_REVIEW, "assistant_learning_error_hash_present", ["Inspect the safe scheduler state error_hash and recent audit/dead-letter records."]
    if int(summary.get("active_bounded_count") or 0) > 0:
        return READY_VERIFIED, "assistant_learning_active_bounded_preferences_ready", []
    return CALIBRATION_PENDING, "assistant_learning_natural_run_clean_but_insufficient_evidence", ["Learning scheduler is production-proven, but bounded preferences are still waiting for enough evidence."]


def _read_recent_learning_runs(db_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM assistant_job_runs
                WHERE job_name = ? AND idempotency_key LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """, (JOB_NAME, f"{RUN_KEY_PREFIX}%", max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _read_open_learning_dead_letters(db_path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM assistant_dead_letters
                WHERE job_name = ? AND status = 'open'
                ORDER BY updated_at DESC LIMIT ?
                """, (JOB_NAME, max(1, int(limit)))).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _read_recent_learning_audit(path: str | Path | None, *, limit: int) -> list[dict[str, Any]]:
    ledger = Path(path) if path else DEFAULT_AUDIT_LEDGER_PATH
    if not ledger.exists():
        return []
    events = []
    for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if str(event.get("workflow") or "") == "assistant_learning_scheduler" or str(event.get("event_type") or "").startswith("assistant.learning"):
            events.append(event)
    return events[-max(1, int(limit)):][::-1]


def _latest_run_for_date(runs: list[dict[str, Any]], expected_date: str) -> dict[str, Any] | None:
    prefix = f"{RUN_KEY_PREFIX}{expected_date}"
    for run in runs:
        if str(run.get("idempotency_key") or "").startswith(prefix):
            return _safe_run(run)
    return None


def _safe_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run.get(key) for key in ["run_id", "job_name", "status", "idempotency_key", "created_at", "updated_at", "failure_class", "summary", "error_hash"]}


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["dead_letter_id", "job_name", "failure_class", "safe_summary", "recovery_hint", "attempts", "last_failed_at", "error_hash"]}


def _safe_preferences(preferences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: pref.get(key) for key in ["preference_key", "lane", "status", "value", "confidence", "evidence_count", "reason", "bounds"]} for pref in preferences]


def _safe_proposals(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: proposal.get(key) for key in ["proposal_id", "preference_key", "status", "proposal_type", "reason", "confidence", "evidence_count"]} for proposal in proposals]


def _audit_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest = events[0] if events else None
    return {"recent_count": len(events), "latest": {key: latest.get(key) for key in ["audit_id", "event_type", "created_at", "decision", "reason"]} if latest else None}


def _human_summary(*, status: str, expected_date: date, reason: str) -> str:
    if status == READY_VERIFIED:
        return f"Assistant learning natural run for {expected_date.isoformat()} is verified and bounded preferences are active."
    if status == CALIBRATION_PENDING:
        return f"Assistant learning natural run for {expected_date.isoformat()} is clean; calibration is pending more evidence."
    if status == READY_PENDING:
        return f"Assistant learning natural run for {expected_date.isoformat()} is still pending its scheduled grace window."
    if status == MANUAL_REVIEW:
        return f"Assistant learning natural run for {expected_date.isoformat()} needs manual review: {reason}."
    return f"Assistant learning is not ready for {expected_date.isoformat()}: {reason}."


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_datetime(value: str | datetime, *, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _default_expected_date(spec: SchedulerJobSpec) -> date:
    if spec.first_expected_run_after:
        return datetime.fromisoformat(spec.first_expected_run_after.replace("Z", "+00:00")).date()
    return datetime.now(ZoneInfo(spec.launchagent.timezone)).date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only readiness gate for Rocky assistant learning.")
    parser.add_argument("--expected-date", dest="expected_date")
    parser.add_argument("--now-local", dest="now_local")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--learning-db", default=str(DEFAULT_LEARNING_DB_PATH), dest="learning_db")
    parser.add_argument("--audit-ledger", dest="audit_ledger")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = evaluate_assistant_learning_readiness(expected_date=args.expected_date, now_local=args.now_local, scheduler_db_path=args.state_db, learning_db_path=args.learning_db, audit_log_path=args.audit_ledger)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload.get("summary") or f"Assistant learning readiness: {payload.get('status')}")
        for hint in payload.get("recovery_hints") or []:
            print(f"- {hint}")
    return 0 if payload.get("status") in {READY_VERIFIED, CALIBRATION_PENDING} else 1


if __name__ == "__main__":
    raise SystemExit(main())
