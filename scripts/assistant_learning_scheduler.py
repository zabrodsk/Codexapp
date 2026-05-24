#!/usr/bin/env python3
"""Scheduler for Rocky assistant outcome learning."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_notification_dispatcher import dispatch_failure_notification
from assistant_outcome_observer import collect_outcomes
from assistant_preference_models import learning_summary, update_preference_models
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso

WORKFLOW = "assistant_learning_scheduler"
JOB_NAME = "assistant_learning"
POLICY_VERSION = "rocky-assistant-learning-scheduler-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/assistant_learning_scheduler.json")
DEFAULT_LEARNING_DB = Path("/Users/clawdbot/.openclaw/workspace/improvement/assistant_learning.sqlite3")
DEFAULT_LOCK_TTL_SECONDS = 1800
SENSITIVE_TEXT_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_assistant_learning_scheduler(*, planning_date: str | date | None = None, since_days: int = 7, live: bool = False, notify_failures: bool = False, notification_dry_run: bool = False, notification_channel_id: str | None = None, learning_db_path: str | Path | None = DEFAULT_LEARNING_DB, scheduler_db_path: str | Path | None = None, calendar_state_db_path: str | Path | None = None, ledger_path: str | Path | None = None, state_file: str | Path | None = DEFAULT_STATE_FILE, lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS, write_audit: bool = True, outcomes_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    run_key = f"assistant-learning:{planning_day.isoformat()}"
    lock = acquire_run_lock(workflow=WORKFLOW, idempotency_key=run_key, ttl_seconds=lock_ttl_seconds, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit, metadata={"job_name": JOB_NAME, "planning_date": planning_day.isoformat(), "live": bool(live)})
    if not lock.acquired:
        return _redact_payload({"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False})
    try:
        outcomes = outcomes_payload or collect_outcomes(since_days=since_days, live=live, learning_db_path=learning_db_path, scheduler_db_path=scheduler_db_path, calendar_state_db_path=calendar_state_db_path, ledger_path=ledger_path, write_audit=write_audit)
        if outcomes.get("status") != "ok":
            return _finish(_base("failed", str(outcomes.get("reason") or "assistant_outcome_collection_failed"), planning_day, run_key, live, outcomes=outcomes), scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
        model_input = outcomes.get("outcomes")
        models = update_preference_models(db_path=learning_db_path, outcomes=model_input, live=live)
        summary = learning_summary(db_path=learning_db_path)
        status = "ok" if int(models.get("active_bounded_count") or 0) > 0 else "skipped_insufficient_evidence"
        if live and int(summary.get("active_bounded_count") or 0) == 0 and int(summary.get("outcome_count") or 0) > 0:
            status = "degraded_no_active_preferences"
        payload = _base(status, str(models.get("reason") or status), planning_day, run_key, live, outcomes=outcomes, models=models, summary=summary)
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=False, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
    except Exception as exc:
        payload = _base("failed", "assistant_learning_scheduler_exception", planning_day, run_key, live)
        payload["error_hash"] = _hash_text(str(exc))
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _base(status: str, reason: str, planning_day: date, run_key: str, live: bool, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "live": bool(live), "calendar_write_attempted": False, "calendar_event_created": False, "calendar_event_deleted": False, "notion_write_attempted": False, **extra}


def _finish(payload: dict[str, Any], *, scheduler_db_path: str | Path | None, ledger_path: str | Path | None, state_file: str | Path | None, write_audit: bool, dead_letter: bool, notify_failures: bool, notification_dry_run: bool, notification_channel_id: str | None) -> dict[str, Any]:
    safe = _redact_payload(payload)
    if state_file:
        _write_state(Path(state_file), safe)
    state = AssistantSchedulerState(scheduler_db_path)
    state.record_job_run(job_name=JOB_NAME, job_label="Rocky assistant learning", scheduled_for=utc_now_iso(), status="dead_lettered" if dead_letter else ("succeeded" if safe.get("status") in {"ok", "degraded_no_active_preferences", "skipped_insufficient_evidence"} else "unknown"), idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), launchagent_label="com.openclaw.rocky-assistant-learning", program="/usr/bin/ssh", failure_class=str(safe.get("reason") or "assistant_learning_failed") if dead_letter else None, summary=str(safe.get("reason") or safe.get("status")), error_hash=safe.get("error_hash"))
    if dead_letter:
        dead = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), failure_class=str(safe.get("reason") or "assistant_learning_failed"), safe_summary=f"Assistant learning {safe.get('status')}: {safe.get('reason')}", source_refs=["assistant-learning:scheduler"], recovery_hint="Inspect assistant-learning-scheduler-run, assistant-learning-summary, and recent dead letters before rerunning.", error_hash=safe.get("error_hash"))
        safe["dead_letter"] = dead
        if notify_failures:
            safe["notification"] = dispatch_failure_notification(safe, channel_id=notification_channel_id, ledger_path=ledger_path, scheduler_db_path=scheduler_db_path, dry_run=notification_dry_run)
    if write_audit:
        event_type = "assistant.learning_degraded" if dead_letter or safe.get("status") == "failed" else "assistant.preference_model_updated"
        event = AssistantAuditLog(ledger_path).record_event(event_type=event_type, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), policy_version=POLICY_VERSION, decision="failed" if dead_letter else "completed", reason=str(safe.get("reason") or safe.get("status")), sources=["assistant_learning_store", "assistant_outcome_observer"], artifacts={"status": safe.get("status"), "summary": safe.get("summary")})
        safe["audit_id"] = event.audit_id
    return safe


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {"last_run_at": utc_now_iso(), "last_status": payload.get("status"), "target_date": payload.get("target_date"), "reason": payload.get("reason"), "outcome_count": (payload.get("outcomes") or {}).get("outcome_count") if isinstance(payload.get("outcomes"), dict) else None, "active_bounded_count": (payload.get("summary") or {}).get("active_bounded_count") if isinstance(payload.get("summary"), dict) else None, "proposal_count": (payload.get("summary") or {}).get("proposal_count") if isinstance(payload.get("summary"), dict) else None, "error_hash": payload.get("error_hash"), "run_idempotency_key": payload.get("run_idempotency_key"), "calendar_write_attempted": False}
    path.write_text(json.dumps(_redact_payload(safe), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    if isinstance(value, str):
        return SENSITIVE_TEXT_RE.sub("[redacted]", value)[:1000]
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(SENSITIVE_TEXT_RE.sub("[redacted]", str(value or "")).encode("utf-8")).hexdigest()[:16]


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky assistant outcome learning scheduler.")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--since-days", type=int, default=7, dest="since_days")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--learning-db", default=str(DEFAULT_LEARNING_DB), dest="learning_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--lock-ttl-seconds", type=int, default=DEFAULT_LOCK_TTL_SECONDS, dest="lock_ttl_seconds")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_assistant_learning_scheduler(planning_date=args.planning_date, since_days=args.since_days, live=args.live, notify_failures=args.notify_failures, notification_dry_run=args.notification_dry_run, notification_channel_id=args.notification_channel_id, learning_db_path=args.learning_db, scheduler_db_path=args.scheduler_db, calendar_state_db_path=args.calendar_state_db, ledger_path=args.ledger_path, state_file=args.state_file, lock_ttl_seconds=args.lock_ttl_seconds, write_audit=args.write_audit)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else f"Assistant learning: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded_no_active_preferences", "skipped_insufficient_evidence", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
