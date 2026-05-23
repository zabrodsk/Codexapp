#!/usr/bin/env python3
"""Automatic scheduler for TrainingPeaks-derived Rocky training blocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
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
from training_calendar_live_booking import book_training_calendar_proposal
from training_calendar_reconciler import reconcile_training_calendar
from training_calendar_proposal_engine import (
    DEFAULT_DAYS_AHEAD,
    DEFAULT_TARGET_WORKING_DAYS,
    DEFAULT_WEBCAL_URL_FILE,
    add_working_days,
    build_training_calendar_proposals,
)


WORKFLOW = "training_calendar_scheduler"
JOB_NAME = "training_calendar_booking"
POLICY_VERSION = "rocky-training-scheduler-v1"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json")
DEFAULT_CALENDAR_NAME = "Calendar"
DEFAULT_MAX_BOOKINGS = 1
DEFAULT_LOCK_TTL_SECONDS = 1800
UNSAFE_TEXT_RE = re.compile(
    r"(webcal://|https?://|cookie|token|secret|password|credential|auth)",
    re.IGNORECASE,
)


def scheduler_idempotency_key(*, planning_day: date, target_day: date) -> str:
    return f"training-calendar-scheduler:{planning_day.isoformat()}:{target_day.isoformat()}"


def run_training_calendar_scheduler(
    *,
    webcal_url_file: str | Path = DEFAULT_WEBCAL_URL_FILE,
    planning_date: str | date | None = None,
    target_working_days: int = DEFAULT_TARGET_WORKING_DAYS,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    max_bookings: int = DEFAULT_MAX_BOOKINGS,
    live: bool = False,
    db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    preview_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    health_payload: dict[str, Any] | None = None,
    reconcile: bool = False,
    fix_safe: bool = False,
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo("Europe/Prague")).date()
    target_day = add_working_days(planning_day, int(target_working_days))
    run_key = scheduler_idempotency_key(planning_day=planning_day, target_day=target_day)
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=lock_ttl_seconds,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={
            "job_name": JOB_NAME,
            "planning_date": planning_day.isoformat(),
            "target_date": target_day.isoformat(),
            "live": bool(live),
        },
    )
    if not lock.acquired:
        payload = _base_payload(
            status="skipped_duplicate_run",
            reason=lock.reason,
            planning_day=planning_day,
            target_day=target_day,
            run_key=run_key,
            live=live,
        )
        payload["lock"] = lock.to_dict()
        return _redact_payload(payload)

    final_payload: dict[str, Any] | None = None
    finish_context = {
        "webcal_url_file": webcal_url_file,
        "planning_date": planning_day.isoformat(),
        "days_ahead": days_ahead,
        "calendar_name": calendar_name,
        "db_path": db_path,
        "calendar_state_db_path": calendar_state_db_path,
        "scheduler_db_path": scheduler_db_path,
        "ledger_path": ledger_path,
        "preview_payload": preview_payload,
        "existing_events": existing_events,
        "health_payload": health_payload,
        "reconcile": bool(reconcile),
        "fix_safe": bool(fix_safe),
        "live": bool(live),
        "notify_failures": bool(notify_failures),
        "notification_dry_run": bool(notification_dry_run),
        "notification_channel_id": notification_channel_id,
    }
    try:
        if target_day.weekday() >= 4:
            final_payload = _base_payload(
                status="skipped_weekend_target",
                reason="proactive_training_target_not_protected_on_friday_saturday_sunday",
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                finish_context=finish_context,
            )

        proposals = build_training_calendar_proposals(
            webcal_url_file=webcal_url_file,
            planning_date=planning_day,
            target_working_days=target_working_days,
            days_ahead=days_ahead,
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
            preview_payload=preview_payload,
            existing_events=existing_events,
        )
        if proposals.get("status") == "blocked" and proposals.get("reason") == "trainingpeaks_read_failed":
            final_payload = _base_payload(
                status="blocked",
                reason="trainingpeaks_read_failed",
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class="trainingpeaks_read_failed",
                finish_context=finish_context,
            )

        all_proposals = list(proposals.get("proposals") or [])
        eligible = [proposal for proposal in all_proposals if proposal.get("status") == "proposal"]
        duplicate_candidates = [
            proposal for proposal in all_proposals
            if proposal.get("reason") == "duplicate_rocky_block" and proposal.get("idempotency_key")
        ]
        bookable = eligible or duplicate_candidates

        if not all_proposals and proposals.get("status") == "no_workout":
            final_payload = _base_payload(
                status="skipped_no_workout",
                reason="no_trainingpeaks_workout_on_target_date",
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                finish_context=finish_context,
            )

        if not eligible and len(duplicate_candidates) == 1:
            selected = duplicate_candidates[0]
            final_payload = _base_payload(
                status="skipped_duplicate",
                reason="duplicate_rocky_block",
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["selected_proposal"] = _safe_selected_proposal(selected)
            final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
            final_payload["idempotency_key"] = selected.get("idempotency_key")
            final_payload["skipped_count"] = 1
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                finish_context=finish_context,
            )

        if len(eligible) > int(max_bookings) or len(bookable) > 1:
            final_payload = _base_payload(
                status="blocked",
                reason="manual_review_required_multiple_training_proposals",
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
            final_payload["candidate_count"] = len(bookable)
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class="manual_review_required_multiple_training_proposals",
                finish_context=finish_context,
            )

        if not bookable:
            final_payload = _base_payload(
                status="blocked",
                reason=str(proposals.get("reason") or "no_bookable_training_proposal"),
                planning_day=planning_day,
                target_day=target_day,
                run_key=run_key,
                live=live,
            )
            final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
            final_payload["lock"] = lock.to_dict()
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class=str(proposals.get("reason") or "no_bookable_training_proposal"),
                finish_context=finish_context,
            )

        selected = bookable[0]
        final_payload = _base_payload(
            status="dry_run_proposal",
            reason="live_flag_not_supplied",
            planning_day=planning_day,
            target_day=target_day,
            run_key=run_key,
            live=live,
        )
        final_payload["selected_proposal"] = _safe_selected_proposal(selected)
        final_payload["proposal_payload"] = _safe_proposal_summary(proposals)
        final_payload["lock"] = lock.to_dict()
        if not live:
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                finish_context=finish_context,
            )

        health = health_payload
        if health is None:
            health = calendar_write_health(
                db_path=db_path,
                ledger_path=ledger_path,
                write_audit=False,
            )
        if health.get("status") != "ok":
            final_payload["status"] = "blocked"
            final_payload["reason"] = "calendar_write_health_not_ok"
            final_payload["blocked_count"] = 1
            final_payload["health"] = _safe_health_summary(health)
            return _finish_run(
                final_payload,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                state_file=state_file,
                write_audit=write_audit,
                dead_letter=True,
                failure_class="calendar_write_health_not_ok",
                finish_context=finish_context,
            )

        booking = book_training_calendar_proposal(
            idempotency_key=str(selected.get("idempotency_key")),
            webcal_url_file=webcal_url_file,
            planning_date=planning_day.isoformat(),
            target_working_days=target_working_days,
            days_ahead=days_ahead,
            calendar_name=calendar_name,
            live=True,
            db_path=db_path,
            state_db_path=calendar_state_db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            preview_payload=preview_payload,
            existing_events=existing_events,
            health_payload=health,
        )
        final_payload["booking_result"] = _safe_booking_summary(booking)
        final_payload["status"] = str(booking.get("status") or "failed")
        final_payload["reason"] = booking.get("reason")
        final_payload["calendar_write_attempted"] = bool(booking.get("calendar_write_attempted"))
        final_payload["calendar_event_created"] = bool(booking.get("calendar_event_created"))
        final_payload["calendar_event_deleted"] = bool(booking.get("calendar_event_deleted"))
        final_payload["audit_id"] = booking.get("audit_id")
        final_payload["idempotency_key"] = selected.get("idempotency_key")
        final_payload["created_count"] = 1 if final_payload["status"] == "created" else 0
        final_payload["skipped_count"] = 1 if final_payload["status"] == "skipped_duplicate" else 0
        final_payload["blocked_count"] = 1 if final_payload["status"] not in {"created", "skipped_duplicate"} else 0
        success_statuses = {"created", "skipped_duplicate"}
        return _finish_run(
            final_payload,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=final_payload["status"] not in success_statuses,
            failure_class=None if final_payload["status"] in success_statuses else str(final_payload["reason"] or "training_calendar_booking_failed"),
            finish_context=finish_context,
        )
    except Exception as exc:
        final_payload = _base_payload(
            status="failed",
            reason="training_calendar_scheduler_exception",
            planning_day=planning_day,
            target_day=target_day,
            run_key=run_key,
            live=live,
        )
        final_payload["error_class"] = exc.__class__.__name__
        final_payload["error_message"] = str(exc)
        final_payload["error_hash"] = _hash_text(str(exc))
        final_payload["lock"] = lock.to_dict()
        return _finish_run(
            final_payload,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            dead_letter=True,
            failure_class="training_calendar_scheduler_exception",
            finish_context=finish_context,
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
    finish_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload, dead_letter, failure_class = _apply_finish_followups(
        payload,
        dead_letter=dead_letter,
        failure_class=failure_class,
        finish_context=finish_context or {},
    )
    safe_payload = _redact_payload(payload)
    status = str(safe_payload.get("status") or "unknown")
    reason = str(safe_payload.get("reason") or status)
    state = AssistantSchedulerState(scheduler_db_path)
    state.record_job_run(
        job_name=JOB_NAME,
        job_label="Rocky training calendar booking",
        scheduled_for=str(safe_payload.get("target_date") or ""),
        status=_job_run_status(status, dead_letter=dead_letter),
        idempotency_key=str(safe_payload.get("run_idempotency_key") or safe_payload.get("idempotency_key") or ""),
        failure_class=failure_class,
        summary=reason,
        error_hash=safe_payload.get("error_hash"),
    )
    if dead_letter:
        dead = state.upsert_dead_letter(
            job_name=JOB_NAME,
            workflow=WORKFLOW,
            idempotency_key=str(safe_payload.get("run_idempotency_key") or safe_payload.get("idempotency_key") or ""),
            failure_class=failure_class or "training_calendar_scheduler_blocked",
            safe_summary=reason,
            source_refs=["trainingpeaks:webcal-secret-file", "apple-calendar:Calendar"],
            recovery_hint="Inspect training-calendar-scheduler output, assistant audit, and calendar-block-status before rerunning.",
            error_hash=safe_payload.get("error_hash"),
        )
        safe_payload["dead_letter"] = dead
        _record_audit(
            ledger_path=ledger_path,
            enabled=write_audit,
            event_type="scheduler.dead_letter_created",
            decision="blocked",
            reason=reason,
            idempotency_key=str(safe_payload.get("run_idempotency_key") or safe_payload.get("idempotency_key") or ""),
            artifacts={"status": status, "failure_class": failure_class, "dead_letter": dead},
        )
    _record_audit(
        ledger_path=ledger_path,
        enabled=write_audit,
        event_type="scheduler.run_observed",
        decision="observed",
        reason=reason,
        idempotency_key=str(safe_payload.get("run_idempotency_key") or safe_payload.get("idempotency_key") or ""),
        artifacts={
            "status": status,
            "target_date": safe_payload.get("target_date"),
            "calendar_write_attempted": safe_payload.get("calendar_write_attempted"),
            "calendar_event_created": safe_payload.get("calendar_event_created"),
        },
    )
    if state_file:
        _write_state_file(Path(state_file), safe_payload)
    notification = _dispatch_failure_notification_if_needed(
        safe_payload,
        enabled=bool((finish_context or {}).get("notify_failures")),
        dry_run=bool((finish_context or {}).get("notification_dry_run")),
        channel_id=(finish_context or {}).get("notification_channel_id"),
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
    )
    if notification:
        safe_payload["notification"] = notification
    return safe_payload


def _apply_finish_followups(
    payload: dict[str, Any],
    *,
    dead_letter: bool,
    failure_class: str | None,
    finish_context: dict[str, Any],
) -> tuple[dict[str, Any], bool, str | None]:
    if not finish_context.get("reconcile"):
        return payload, dead_letter, failure_class
    reconcile_payload = reconcile_training_calendar(
        webcal_url_file=finish_context.get("webcal_url_file", DEFAULT_WEBCAL_URL_FILE),
        planning_date=finish_context.get("planning_date"),
        days_ahead=int(finish_context.get("days_ahead") or DEFAULT_DAYS_AHEAD),
        calendar_name=finish_context.get("calendar_name") or DEFAULT_CALENDAR_NAME,
        fix_safe=bool(finish_context.get("fix_safe")),
        live=bool(finish_context.get("live")),
        notify_failures=False,
        db_path=finish_context.get("db_path"),
        state_db_path=finish_context.get("calendar_state_db_path"),
        scheduler_db_path=finish_context.get("scheduler_db_path"),
        ledger_path=finish_context.get("ledger_path"),
        preview_payload=finish_context.get("preview_payload"),
        existing_events=finish_context.get("existing_events"),
        health_payload=finish_context.get("health_payload"),
    )
    payload["reconcile_result"] = _safe_reconcile_summary(reconcile_payload)
    payload["calendar_write_attempted"] = bool(payload.get("calendar_write_attempted")) or bool(reconcile_payload.get("calendar_write_attempted"))
    if reconcile_payload.get("status") in {"blocked", "failed", "manual_review_required"}:
        payload["status"] = "blocked"
        payload["reason"] = "training_calendar_reconcile_attention_needed"
        payload["blocked_count"] = 1
        payload["manual_review_required"] = True
        dead_letter = True
        failure_class = str(reconcile_payload.get("reason") or "training_calendar_reconcile_attention_needed")
    return payload, dead_letter, failure_class


def _safe_reconcile_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "manual_review_required": payload.get("manual_review_required"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "result_statuses": [result.get("status") for result in payload.get("results", [])],
        }
    )


def _dispatch_failure_notification_if_needed(
    payload: dict[str, Any],
    *,
    enabled: bool,
    dry_run: bool,
    channel_id: str | None,
    ledger_path: str | Path | None,
    scheduler_db_path: str | Path | None,
) -> dict[str, Any] | None:
    if not enabled:
        return None
    return dispatch_failure_notification(
        payload,
        channel_id=channel_id or "1485710572325703901",
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        dry_run=dry_run,
    )


def _base_payload(
    *,
    status: str,
    reason: str | None,
    planning_day: date,
    target_day: date,
    run_key: str,
    live: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "workflow": WORKFLOW,
        "job_name": JOB_NAME,
        "mode": "live" if live else "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": target_day.isoformat(),
        "target_weekday": target_day.isoweekday(),
        "run_idempotency_key": run_key,
        "idempotency_key": None,
        "calendar_name": DEFAULT_CALENDAR_NAME,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "created_count": 0,
        "skipped_count": 0 if status not in {"skipped_no_workout", "skipped_weekend_target", "skipped_duplicate_run"} else 1,
        "blocked_count": 1 if status in {"blocked", "failed"} else 0,
        "checked_at": utc_now_iso(),
    }


def _safe_health_summary(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": health.get("status"),
        "blocked_checks": list(health.get("blocked_checks") or []),
        "calendar_write_attempted": bool(health.get("calendar_write_attempted")),
    }


def _safe_proposal_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "planning_date": payload.get("planning_date"),
            "target_date": payload.get("target_date"),
            "selected_workout_count": payload.get("selected_workout_count"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "proposal_count": len(payload.get("proposals") or []),
            "idempotency_keys": [
                proposal.get("idempotency_key")
                for proposal in payload.get("proposals", [])
                if proposal.get("idempotency_key")
            ],
        }
    )


def _safe_selected_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    workout = proposal.get("workout") or {}
    inference = proposal.get("inference") or {}
    return _redact_payload(
        {
            "status": proposal.get("status"),
            "reason": proposal.get("reason"),
            "idempotency_key": proposal.get("idempotency_key"),
            "workout": {
                "source": workout.get("source"),
                "source_ref": workout.get("source_ref"),
                "date": workout.get("date"),
                "title": workout.get("title"),
                "sport": workout.get("sport"),
                "confidence": workout.get("confidence"),
                "warnings": list(workout.get("warnings") or []),
            },
            "inference": {
                "window_start": inference.get("window_start"),
                "window_end": inference.get("window_end"),
                "proposal_duration_minutes": inference.get("proposal_duration_minutes"),
                "confidence": inference.get("confidence"),
                "warnings": list(inference.get("warnings") or []),
            },
        }
    )


def _safe_booking_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "mode": payload.get("mode"),
            "idempotency_key": payload.get("idempotency_key"),
            "audit_id": payload.get("audit_id"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "calendar_event_created": payload.get("calendar_event_created"),
            "calendar_event_deleted": payload.get("calendar_event_deleted"),
        }
    )


def _write_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run_at": payload.get("checked_at"),
        "last_status": payload.get("status"),
        "target_date": payload.get("target_date"),
        "idempotency_key": payload.get("idempotency_key"),
        "run_idempotency_key": payload.get("run_idempotency_key"),
        "reason": payload.get("reason"),
        "created_count": int(bool(payload.get("calendar_event_created"))),
        "skipped_count": int(payload.get("skipped_count") or payload.get("status") in {"skipped_duplicate", "skipped_no_workout", "skipped_weekend_target"}),
        "blocked_count": int(payload.get("blocked_count") or payload.get("status") in {"blocked", "failed"}),
        "error_hash": payload.get("error_hash"),
    }
    path.write_text(json.dumps(_redact_payload(state), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _record_audit(
    *,
    ledger_path: str | Path | None,
    enabled: bool,
    event_type: str,
    decision: str,
    reason: str,
    idempotency_key: str,
    artifacts: dict[str, Any],
) -> None:
    if not enabled:
        return
    AssistantAuditLog(ledger_path).record_event(
        event_type=event_type,
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision=decision,
        reason=reason,
        sources=["trainingpeaks:webcal-secret-file", "apple-calendar:Calendar"],
        artifacts=_redact_payload(artifacts),
    )


def _job_run_status(status: str, *, dead_letter: bool) -> str:
    if dead_letter:
        return "dead_lettered"
    if status.startswith("skipped") or status == "dry_run_proposal":
        return "succeeded"
    if status == "created":
        return "succeeded"
    return "succeeded" if status == "skipped_duplicate" else "unknown"


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {
            "redacted": True,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
            "chars": len(value),
        }
    return value


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky automatic TrainingPeaks calendar booking.")
    parser.add_argument("--webcal-url-file", default=str(DEFAULT_WEBCAL_URL_FILE), dest="webcal_url_file")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--target-working-days", type=int, default=DEFAULT_TARGET_WORKING_DAYS, dest="target_working_days")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD, dest="days_ahead")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR_NAME, dest="calendar_name")
    parser.add_argument("--max-bookings", type=int, default=DEFAULT_MAX_BOOKINGS, dest="max_bookings")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--lock-ttl-seconds", type=int, default=DEFAULT_LOCK_TTL_SECONDS, dest="lock_ttl_seconds")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reconcile", action="store_true", help="Run TrainingPeaks/Calendar reconciliation after booking.")
    parser.add_argument("--fix-safe", action="store_true", dest="fix_safe", help="Apply narrowly safe reconciliation fixes when --live is set.")
    parser.add_argument("--notify-failures", action="store_true", dest="notify_failures", help="Send failure/manual-review notifications.")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_training_calendar_scheduler(
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        target_working_days=args.target_working_days,
        days_ahead=args.days_ahead,
        calendar_name=args.calendar_name,
        max_bookings=args.max_bookings,
        live=args.live,
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
        reconcile=args.reconcile,
        fix_safe=args.fix_safe,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar scheduler: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Target date: {payload.get('target_date')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
    return 0 if payload.get("status") in {"created", "skipped_duplicate", "skipped_no_workout", "skipped_weekend_target", "dry_run_proposal"} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_STATE_FILE",
    "JOB_NAME",
    "WORKFLOW",
    "run_training_calendar_scheduler",
    "scheduler_idempotency_key",
]
