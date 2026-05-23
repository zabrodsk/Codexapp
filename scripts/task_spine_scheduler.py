#!/usr/bin/env python3
"""Automatic task-spine scheduler for Rocky."""
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
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from notion_task_manager import ensure_task_database_schema, load_notion_task_config, notion_task_health, upsert_task
from task_deduper import dedupe_task_candidates
from task_detector import detect_task_candidates
from task_focus_live_booking import book_task_focus_proposal
from task_focus_proposal_engine import build_task_focus_proposals
from task_reminder_engine import run_task_reminders
from task_signal_collector import collect_task_signals


WORKFLOW = "task_spine_scheduler"
JOB_NAME = "task_spine"
POLICY_VERSION = "rocky-task-spine-scheduler-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/task_spine_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 1800
UNSAFE_TEXT_RE = re.compile(r"(https?://|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_task_spine_scheduler(
    *,
    planning_date: str | date | None = None,
    live: bool = False,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    sources: list[str] | None = None,
    since_days: int = 7,
    limit: int = 30,
    db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    helper_payload: dict[str, Any] | None = None,
    memory_results: list[dict[str, Any]] | None = None,
    meeting_results: list[dict[str, Any]] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    notion_client: Any | None = None,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    run_key = f"task-spine:{planning_day.isoformat()}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=lock_ttl_seconds,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"job_name": JOB_NAME, "planning_date": planning_day.isoformat(), "live": bool(live)},
    )
    if not lock.acquired:
        return _redact_payload(
            {
                "status": "skipped_duplicate_run",
                "reason": lock.reason,
                "workflow": WORKFLOW,
                "run_idempotency_key": run_key,
                "lock": lock.to_dict(),
                "calendar_write_attempted": False,
                "notion_write_attempted": False,
            }
        )
    try:
        payload = _run_inner(
            planning_day=planning_day,
            live=live,
            notify=notify,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
            sources=sources,
            since_days=since_days,
            limit=limit,
            db_path=db_path,
            calendar_state_db_path=calendar_state_db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            helper_payload=helper_payload,
            memory_results=memory_results,
            meeting_results=meeting_results,
            existing_events=existing_events,
            notion_client=notion_client,
            llm_func=llm_func,
            run_key=run_key,
        )
        return payload
    except Exception as exc:
        payload = {
            "status": "failed",
            "reason": "task_spine_scheduler_exception",
            "workflow": WORKFLOW,
            "target_date": planning_day.isoformat(),
            "run_idempotency_key": run_key,
            "error_class": exc.__class__.__name__,
            "error_hash": _hash_text(str(exc)),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
        return _finish_run(
            payload,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=True,
            failure_class="task_spine_scheduler_exception",
            notify=notify,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
        )
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _run_inner(
    *,
    planning_day: date,
    live: bool,
    notify: bool,
    notification_dry_run: bool,
    notification_channel_id: str | None,
    sources: list[str] | None,
    since_days: int,
    limit: int,
    db_path: str | Path | None,
    calendar_state_db_path: str | Path | None,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    state_file: str | Path | None,
    write_audit: bool,
    helper_payload: dict[str, Any] | None,
    memory_results: list[dict[str, Any]] | None,
    meeting_results: list[dict[str, Any]] | None,
    existing_events: list[dict[str, Any]] | None,
    notion_client: Any | None,
    llm_func: Any | None,
    run_key: str,
) -> dict[str, Any]:
    config = load_notion_task_config()
    health = notion_task_health(config)
    if health["status"] != "ok":
        return _finish_run(
            _base_payload("blocked", str(health.get("reason") or "notion_task_health_blocked"), planning_day, run_key, live),
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=True,
            failure_class=str(health.get("reason") or "notion_task_health_blocked"),
            notify=notify,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
        )
    schema = ensure_task_database_schema(live=live, config=config, client=notion_client)
    signals = collect_task_signals(
        sources=sources,
        since_days=since_days,
        limit=limit,
        helper_payload=helper_payload,
        memory_results=memory_results,
        meeting_results=meeting_results,
    )
    detected = detect_task_candidates(signals.get("signals") or [], use_llm=True, llm_func=llm_func, max_candidates=limit)
    deduped = dedupe_task_candidates(detected.get("candidates") or [])
    upserts = []
    created_tasks = []
    candidate_tasks = []
    for task in deduped.get("candidates") or []:
        if task.get("auto_create_allowed"):
            result = upsert_task(task, live=live, config=config, client=notion_client)
            upserts.append(result)
            created_tasks.append({**task, "page_id": result.get("page_id")})
        elif float(task.get("confidence") or 0) >= 0.6 and not task.get("prompt_injection_flagged"):
            candidate_tasks.append({**task, "status": "Candidate"})
    reminders = run_task_reminders(
        today=planning_day,
        tasks=[*created_tasks, *candidate_tasks],
        notify=notify and live,
        notification_dry_run=notification_dry_run,
        notification_channel_id=notification_channel_id,
        ledger_path=str(ledger_path) if ledger_path else None,
        scheduler_db_path=str(scheduler_db_path) if scheduler_db_path else None,
    )
    focus = build_task_focus_proposals(
        planning_date=planning_day,
        tasks=created_tasks,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        existing_events=existing_events,
    )
    booking = None
    if live and focus.get("status") == "proposal" and focus.get("idempotency_key"):
        booking = book_task_focus_proposal(
            idempotency_key=str(focus["idempotency_key"]),
            planning_date=planning_day.isoformat(),
            calendar_name="Calendar",
            live=True,
            tasks=created_tasks,
            db_path=db_path,
            state_db_path=calendar_state_db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            existing_events=existing_events,
            notion_client=notion_client,
            notion_config=config,
        )
    status = "ok"
    reason = "task_spine_completed"
    if signals.get("status") == "degraded" or detected.get("status") == "degraded":
        status = "degraded"
        reason = "task_spine_completed_with_degraded_signals"
    payload = _base_payload(status, reason, planning_day, run_key, live)
    payload.update(
        {
            "schema": _safe_schema(schema),
            "signal_count": signals.get("signal_count", 0),
            "candidate_count": detected.get("candidate_count", 0),
            "deduped_count": deduped.get("candidate_count", 0),
            "notion_upsert_count": len(upserts),
            "auto_created_count": len(created_tasks),
            "review_candidate_count": len(candidate_tasks),
            "reminders": reminders,
            "task_focus": focus,
            "booking_result": booking,
            "calendar_write_attempted": bool(booking and booking.get("calendar_write_attempted")),
            "calendar_event_created": bool(booking and booking.get("calendar_event_created")),
            "notion_write_attempted": any(item.get("notion_write_attempted") for item in upserts) or bool(schema.get("notion_write_attempted")),
        }
    )
    return _finish_run(
        payload,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
        state_file=state_file,
        write_audit=write_audit,
        dead_letter=False,
        failure_class=None,
        notify=False,
        notification_dry_run=notification_dry_run,
        notification_channel_id=notification_channel_id,
    )


def _finish_run(
    payload: dict[str, Any],
    *,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    state_file: str | Path | None,
    write_audit: bool,
    dead_letter: bool = False,
    failure_class: str | None = None,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
) -> dict[str, Any]:
    safe = _redact_payload(payload)
    state = AssistantSchedulerState(scheduler_db_path)
    state.record_job_run(
        job_name=JOB_NAME,
        job_label="Rocky task spine",
        scheduled_for=str(safe.get("target_date") or ""),
        status="dead_lettered" if dead_letter else "succeeded",
        idempotency_key=str(safe.get("run_idempotency_key") or ""),
        failure_class=failure_class,
        summary=str(safe.get("reason") or safe.get("status")),
        error_hash=safe.get("error_hash"),
    )
    if dead_letter:
        safe["dead_letter"] = state.upsert_dead_letter(
            job_name=JOB_NAME,
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or ""),
            failure_class=failure_class or "task_spine_failed",
            safe_summary=str(safe.get("reason") or "task spine failed"),
            source_refs=["notion:task-spine", "obsidian:layer3", "apple-mail:unread-inbox"],
            recovery_hint="Inspect task-spine scheduler output, Notion task health, and assistant audit before rerunning.",
            error_hash=safe.get("error_hash"),
        )
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="scheduler.run_observed",
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or ""),
            policy_version=POLICY_VERSION,
            decision="blocked" if dead_letter else "observed",
            reason=str(safe.get("reason") or safe.get("status")),
            sources=["notion:task-spine", "obsidian:layer3", "apple-mail:unread-inbox"],
            artifacts={
                "status": safe.get("status"),
                "signal_count": safe.get("signal_count"),
                "candidate_count": safe.get("candidate_count"),
                "notion_upsert_count": safe.get("notion_upsert_count"),
                "calendar_write_attempted": safe.get("calendar_write_attempted"),
                "calendar_event_created": safe.get("calendar_event_created"),
            },
        )
        safe["audit_id"] = event.audit_id
    if state_file:
        _write_state_file(Path(state_file), safe)
    if notify and dead_letter:
        safe["notification"] = dispatch_failure_notification(
            safe,
            channel_id=notification_channel_id or "1485710572325703901",
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            dry_run=notification_dry_run,
        )
    return safe


def _base_payload(status: str, reason: str | None, planning_day: date, run_key: str, live: bool) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "workflow": WORKFLOW,
        "job_name": JOB_NAME,
        "mode": "live" if live else "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": planning_day.isoformat(),
        "run_idempotency_key": run_key,
        "created_count": 0,
        "blocked_count": 1 if status in {"blocked", "failed"} else 0,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "notion_write_attempted": False,
        "checked_at": utc_now_iso(),
    }


def _safe_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": schema.get("status"),
        "reason": schema.get("reason"),
        "database_configured": bool(schema.get("database_id")),
        "notion_write_attempted": bool(schema.get("notion_write_attempted")),
    }


def _write_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_state = {
        "last_run_at": utc_now_iso(),
        "last_status": payload.get("status"),
        "target_date": payload.get("target_date"),
        "run_idempotency_key": payload.get("run_idempotency_key"),
        "reason": payload.get("reason"),
        "signal_count": payload.get("signal_count", 0),
        "candidate_count": payload.get("candidate_count", 0),
        "notion_upsert_count": payload.get("notion_upsert_count", 0),
        "calendar_write_attempted": payload.get("calendar_write_attempted", False),
        "calendar_event_created": payload.get("calendar_event_created", False),
        "error_hash": payload.get("error_hash"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items() if str(key).lower() not in {"body", "content", "raw", "transcript"}}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": _hash_text(value), "chars": len(value)}
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky task spine scheduler.")
    parser.add_argument("--planning-date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--state-db", dest="calendar_state_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--no-write-audit", action="store_true", dest="no_write_audit")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_task_spine_scheduler(
        planning_date=args.planning_date,
        live=args.live,
        notify=args.notify,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        sources=args.sources,
        since_days=args.since_days,
        limit=args.limit,
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        write_audit=not args.no_write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Task spine scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
