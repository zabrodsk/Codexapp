#!/usr/bin/env python3
"""Automatic scheduler for Rocky email triage calendar blocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_calendar_status import calendar_write_health
from assistant_notification_dispatcher import dispatch_failure_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from email_triage_live_booking import book_email_triage_proposal
from email_triage_proposal_engine import TIMEZONE, build_email_triage_proposals


WORKFLOW = "email_triage_scheduler"
JOB_NAME = "email_triage_booking"
POLICY_VERSION = "rocky-email-triage-scheduler-v1"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/email_triage_scheduler.json")
DEFAULT_CALENDAR_NAME = "Calendar"
DEFAULT_LOCK_TTL_SECONDS = 1800
UNSAFE_TEXT_RE = re.compile(r"(https?://|cookie|token|secret|password|credential|auth|Bearer\s+|sk-)", re.IGNORECASE)


def scheduler_idempotency_key(*, planning_day: date) -> str:
    return f"email-triage-scheduler:{planning_day.isoformat()}"


def run_email_triage_scheduler(
    *,
    planning_date: str | date | None = None,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    live: bool = False,
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    hours: int = 168,
    limit: int = 100,
    db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    helper_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    health_payload: dict[str, Any] | None = None,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    run_key = scheduler_idempotency_key(planning_day=planning_day)
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
        payload = _base_payload(
            status="skipped_duplicate_run",
            reason=lock.reason,
            planning_day=planning_day,
            run_key=run_key,
            live=live,
        )
        payload["lock"] = lock.to_dict()
        return _redact_payload(payload)

    try:
        if planning_day != now.date():
            return _finish_run(
                _base_payload(
                    status="blocked",
                    reason="email_triage_must_be_same_day",
                    planning_day=planning_day,
                    run_key=run_key,
                    live=live,
                ),
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class="email_triage_must_be_same_day",
                notify_failures=notify_failures,
                notification_dry_run=notification_dry_run,
                notification_channel_id=notification_channel_id,
            )
        if planning_day.weekday() >= 4:
            return _finish_run(
                _base_payload(
                    status="skipped_weekend_target",
                    reason="proactive_booking_blocked_on_friday_saturday_sunday",
                    planning_day=planning_day,
                    run_key=run_key,
                    live=live,
                ),
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
            )
        if live and now.time() < time(8, 0):
            return _finish_run(
                _base_payload(
                    status="skipped_before_morning",
                    reason="email_triage_not_before_morning",
                    planning_day=planning_day,
                    run_key=run_key,
                    live=live,
                ),
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
            )

        proposals = build_email_triage_proposals(
            planning_date=planning_day,
            hours=hours,
            limit=limit,
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
            helper_payload=helper_payload,
            existing_events=existing_events,
            now_local=now,
        )
        if proposals.get("status") == "skipped_no_attention_emails":
            payload = _base_payload(
                status="skipped_no_attention_emails",
                reason="no_unread_attention_emails",
                planning_day=planning_day,
                run_key=run_key,
                live=live,
            )
            payload["proposal_payload"] = _safe_proposal_summary(proposals)
            payload["skipped_count"] = 1
            return _finish_run(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)
        if proposals.get("status") == "skipped_duplicate":
            selected = proposals.get("selected_proposal") or {}
            payload = _base_payload(
                status="skipped_duplicate",
                reason="duplicate_rocky_block",
                planning_day=planning_day,
                run_key=run_key,
                live=live,
            )
            payload["selected_proposal"] = _safe_selected_proposal(selected)
            payload["proposal_payload"] = _safe_proposal_summary(proposals)
            payload["idempotency_key"] = selected.get("idempotency_key")
            payload["skipped_count"] = 1
            return _finish_run(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)
        if proposals.get("status") != "proposal":
            payload = _base_payload(
                status="blocked",
                reason=str(proposals.get("reason") or "email_triage_proposal_blocked"),
                planning_day=planning_day,
                run_key=run_key,
                live=live,
            )
            payload["proposal_payload"] = _safe_proposal_summary(proposals)
            payload["blocked_count"] = 1
            return _finish_run(
                payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class=str(proposals.get("reason") or "email_triage_proposal_blocked"),
                notify_failures=notify_failures,
                notification_dry_run=notification_dry_run,
                notification_channel_id=notification_channel_id,
            )

        selected = proposals.get("selected_proposal") or {}
        payload = _base_payload(
            status="dry_run_proposal",
            reason="live_flag_not_supplied",
            planning_day=planning_day,
            run_key=run_key,
            live=live,
        )
        payload["selected_proposal"] = _safe_selected_proposal(selected)
        payload["proposal_payload"] = _safe_proposal_summary(proposals)
        payload["idempotency_key"] = selected.get("idempotency_key")
        if not live:
            return _finish_run(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)

        health = health_payload
        if health is None:
            health = calendar_write_health(db_path=db_path, ledger_path=ledger_path, write_audit=False)
        if health.get("status") != "ok":
            payload["status"] = "blocked"
            payload["reason"] = "calendar_write_health_not_ok"
            payload["blocked_count"] = 1
            payload["health"] = {"status": health.get("status"), "blocked_checks": list(health.get("blocked_checks") or [])}
            return _finish_run(
                payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class="calendar_write_health_not_ok",
                notify_failures=notify_failures,
                notification_dry_run=notification_dry_run,
                notification_channel_id=notification_channel_id,
            )

        booking = book_email_triage_proposal(
            idempotency_key=str(selected.get("idempotency_key")),
            planning_date=planning_day.isoformat(),
            calendar_name=calendar_name,
            live=True,
            hours=hours,
            limit=limit,
            db_path=db_path,
            state_db_path=calendar_state_db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            helper_payload=helper_payload,
            existing_events=existing_events,
            proposal_payload=proposals,
            health_payload=health,
            now_local=now,
        )
        payload["booking_result"] = _safe_booking_summary(booking)
        payload["status"] = str(booking.get("status") or "failed")
        payload["reason"] = booking.get("reason")
        payload["calendar_write_attempted"] = bool(booking.get("calendar_write_attempted"))
        payload["calendar_event_created"] = bool(booking.get("calendar_event_created"))
        payload["calendar_event_deleted"] = bool(booking.get("calendar_event_deleted"))
        payload["audit_id"] = booking.get("audit_id")
        payload["created_count"] = 1 if payload["status"] == "created" else 0
        payload["skipped_count"] = 1 if payload["status"] == "skipped_duplicate" else 0
        payload["blocked_count"] = 1 if payload["status"] not in {"created", "skipped_duplicate"} else 0
        return _finish_run(
            payload,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=payload["status"] not in {"created", "skipped_duplicate"},
            failure_class=None if payload["status"] in {"created", "skipped_duplicate"} else str(payload["reason"] or "email_triage_booking_failed"),
            notify_failures=notify_failures,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
        )
    except Exception as exc:
        payload = _base_payload(
            status="failed",
            reason="email_triage_scheduler_exception",
            planning_day=planning_day,
            run_key=run_key,
            live=live,
        )
        payload["error_class"] = exc.__class__.__name__
        payload["error_hash"] = _hash_text(str(exc))
        return _finish_run(
            payload,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=True,
            failure_class="email_triage_scheduler_exception",
            notify_failures=notify_failures,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
        )
    finally:
        release_run_lock(
            workflow=WORKFLOW,
            idempotency_key=run_key,
            db_path=scheduler_db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
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
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
) -> dict[str, Any]:
    safe = _redact_payload(payload)
    state = AssistantSchedulerState(scheduler_db_path)
    status = str(safe.get("status") or "unknown")
    reason = str(safe.get("reason") or status)
    if dead_letter:
        dead = state.upsert_dead_letter(
            job_name=JOB_NAME,
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or safe.get("idempotency_key") or ""),
            failure_class=failure_class or "email_triage_scheduler_blocked",
            safe_summary=reason,
            source_refs=["apple-mail:unread-inbox", "apple-calendar:Calendar"],
            recovery_hint="Inspect email-triage-scheduler output, assistant audit, and calendar availability before rerunning.",
            error_hash=safe.get("error_hash"),
        )
        safe["dead_letter"] = dead
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="scheduler.run_observed",
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or safe.get("idempotency_key") or ""),
            policy_version=POLICY_VERSION,
            decision="blocked" if dead_letter else "observed",
            reason=reason,
            sources=["apple-mail:unread-inbox", "apple-calendar:Calendar"],
            artifacts={
                "status": status,
                "target_date": safe.get("target_date"),
                "calendar_write_attempted": safe.get("calendar_write_attempted"),
                "calendar_event_created": safe.get("calendar_event_created"),
                "failure_class": failure_class,
            },
        )
        safe.setdefault("audit_id", event.audit_id)
    if notify_failures:
        safe["notification"] = dispatch_failure_notification(
            safe,
            channel_id=notification_channel_id or "1485710572325703901",
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            dry_run=notification_dry_run,
        )
    if state_file:
        _write_state_file(Path(state_file), safe)
    handled_business_block = _handled_business_block(safe, failure_class=failure_class)
    state.record_job_run(
        job_name=JOB_NAME,
        job_label="Rocky email triage booking",
        scheduled_for=str(safe.get("target_date") or ""),
        status=_job_run_status(status, dead_letter=dead_letter, handled_business_block=handled_business_block),
        idempotency_key=str(safe.get("run_idempotency_key") or safe.get("idempotency_key") or ""),
        failure_class=None if handled_business_block else failure_class,
        summary=reason,
        error_hash=safe.get("error_hash"),
    )
    return safe


def _base_payload(*, status: str, reason: str | None, planning_day: date, run_key: str, live: bool) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "workflow": WORKFLOW,
        "job_name": JOB_NAME,
        "mode": "live" if live else "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": planning_day.isoformat(),
        "target_weekday": planning_day.isoweekday(),
        "run_idempotency_key": run_key,
        "idempotency_key": None,
        "calendar_name": DEFAULT_CALENDAR_NAME,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "created_count": 0,
        "skipped_count": 1 if status.startswith("skipped") else 0,
        "blocked_count": 1 if status in {"blocked", "failed"} else 0,
        "checked_at": utc_now_iso(),
    }


def _safe_proposal_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "planning_date": payload.get("planning_date"),
            "target_date": payload.get("target_date"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "email_attention": payload.get("email_attention"),
            "estimate": payload.get("estimate"),
            "proposal_count": len(payload.get("proposals") or []),
            "idempotency_keys": [
                item.get("idempotency_key")
                for item in payload.get("proposals", [])
                if item.get("idempotency_key")
            ],
        }
    )


def _safe_selected_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": proposal.get("status"),
            "reason": proposal.get("reason"),
            "idempotency_key": proposal.get("idempotency_key"),
            "audit_id": proposal.get("audit_id"),
            "proposal": proposal.get("proposal"),
            "email_attention": proposal.get("email_attention"),
            "estimate": proposal.get("estimate"),
        }
    )


def _safe_booking_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "idempotency_key": payload.get("idempotency_key"),
            "audit_id": payload.get("audit_id"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "calendar_event_created": payload.get("calendar_event_created"),
            "calendar_event_deleted": payload.get("calendar_event_deleted"),
        }
    )


def _handled_business_block(payload: dict[str, Any], *, failure_class: str | None) -> bool:
    if failure_class != "no_available_slot":
        return False
    notification = payload.get("notification") or {}
    return notification.get("status") in {"posted", "dry_run"}


def _job_run_status(status: str, *, dead_letter: bool, handled_business_block: bool = False) -> str:
    if handled_business_block:
        return "succeeded"
    if dead_letter:
        return "dead_lettered"
    if status.startswith("skipped") or status in {"created", "dry_run_proposal"}:
        return "succeeded"
    return "unknown"


def _write_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_state = {
        "last_run_at": utc_now_iso(),
        "last_status": payload.get("status"),
        "target_date": payload.get("target_date"),
        "idempotency_key": payload.get("idempotency_key"),
        "run_idempotency_key": payload.get("run_idempotency_key"),
        "reason": payload.get("reason"),
        "created_count": payload.get("created_count", 0),
        "skipped_count": payload.get("skipped_count", 0),
        "blocked_count": payload.get("blocked_count", 0),
        "notification_status": (payload.get("notification") or {}).get("status"),
        "error_hash": payload.get("error_hash"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(TIMEZONE)) if value.tzinfo else value.replace(tzinfo=ZoneInfo(TIMEZONE))
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo(TIMEZONE)) if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return datetime.now(ZoneInfo(TIMEZONE))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items() if str(key) not in {"description", "body", "raw", "content"}}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": _hash_text(value), "chars": len(value)}
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky email triage scheduler.")
    parser.add_argument("--planning-date")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR_NAME, dest="calendar_name")
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--no-write-audit", action="store_true", dest="no_write_audit")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_email_triage_scheduler(
        planning_date=args.planning_date,
        calendar_name=args.calendar_name,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        hours=args.hours,
        limit=args.limit,
        db_path=args.db_path,
        calendar_state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        write_audit=not args.no_write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Email triage scheduler: {payload.get('status')} ({payload.get('reason')})")
    handled_business_block = (
        payload.get("status") == "blocked"
        and payload.get("reason") == "no_available_slot"
        and (payload.get("notification") or {}).get("status") in {"posted", "dry_run"}
    )
    return 0 if payload.get("status") in {"created", "skipped_duplicate", "skipped_no_attention_emails", "skipped_weekend_target", "skipped_before_morning", "dry_run_proposal"} or handled_business_block else 1


if __name__ == "__main__":
    raise SystemExit(main())
