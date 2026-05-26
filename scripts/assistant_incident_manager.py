#!/usr/bin/env python3
"""Incident manager for Rocky assistant dead letters.

This layer sits above the existing scheduler dead-letter table. It makes
recoverable failures visible, asks Dusan for guidance when automation is blocked
by business constraints, and marks incidents recovered only after a verified
retry or compensating notification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog, redact_payload
from assistant_notification_dispatcher import (
    DEFAULT_ALERT_CHANNEL_ID,
    DEFAULT_FALLBACK_EMAIL,
    dispatch_failure_notification,
    dispatch_user_notification,
)
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from daily_personal_briefing_scheduler import run_daily_personal_briefing
from weekly_personal_review_scheduler import build_weekly_personal_review


WORKFLOW = "assistant_incident_manager"
JOB_NAME = "assistant_incident_manager"
POLICY_VERSION = "rocky-incident-manager-v1"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/assistant_incident_manager.json")
DEFAULT_LOCK_TTL_SECONDS = 900
INCIDENT_STATUSES = {"open", "notified", "waiting_for_user", "retrying", "recovered", "acknowledged", "ignored"}
RESPOND_ACTIONS = {"acknowledge": "acknowledged", "ignore": "ignored", "recover": "recovered"}
TIMEZONE = "Europe/Prague"


def run_incident_manager(
    *,
    live: bool = False,
    limit: int = 20,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    notification_dry_run: bool = False,
    quiet_minutes: int = 240,
    post_func: Any | None = None,
    agentmail_send_func: Any | None = None,
    write_audit: bool = True,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    run_key = f"assistant-incident-manager:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M')}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=DEFAULT_LOCK_TTL_SECONDS,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"job_name": JOB_NAME, "live": bool(live)},
    )
    if not lock.acquired:
        return redact_payload({"status": "skipped_duplicate_run", "reason": lock.reason, "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False})
    try:
        state = AssistantSchedulerState(scheduler_db_path)
        candidates = sorted(
            [item for item in state.list_dead_letters(status=None, limit=limit * 4) if item.get("status") in {"open", "notified", "waiting_for_user"}],
            key=_incident_priority,
        )
        processed: list[dict[str, Any]] = []
        for item in candidates[:limit]:
            if not _should_process(item, quiet_minutes=quiet_minutes):
                processed.append(_incident_summary(item, action="skipped_quiet_window"))
                continue
            if not live:
                processed.append(_incident_summary(item, action="dry_run"))
                continue
            processed.append(_process_incident(
                item,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
                channel_id=channel_id,
                fallback_email=fallback_email,
                notification_dry_run=notification_dry_run,
                post_func=post_func,
                agentmail_send_func=agentmail_send_func,
                write_audit=write_audit,
                now_local=now_local,
            ))
        status = "ok"
        if any(item.get("status") in {"waiting_for_user", "notified"} for item in processed):
            status = "manual_review_required"
        if any(item.get("status") == "failed" for item in processed):
            status = "degraded"
        payload = {
            "status": status,
            "reason": "assistant_incident_manager_completed",
            "run_idempotency_key": run_key,
            "processed_count": len(processed),
            "processed": processed,
            "state_mutated": live,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
            "notification_sent": any((item.get("notification") or {}).get("status") == "posted" for item in processed),
        }
        _write_state(state_file, payload)
        AssistantSchedulerState(scheduler_db_path).record_job_run(
            job_name=JOB_NAME,
            job_label="Rocky incident manager",
            status="succeeded" if status in {"ok", "manual_review_required"} else "degraded",
            idempotency_key=run_key,
            finished_at=utc_now_iso(),
            launchagent_label="com.openclaw.rocky-incident-manager",
            program="assistant_incident_manager.py",
            summary=json.dumps(redact_payload({"status": status, "processed_count": len(processed)}), sort_keys=True),
        )
        return redact_payload(payload)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def retry_incident(
    *,
    dead_letter_id: str,
    live: bool = False,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    notification_dry_run: bool = False,
    post_func: Any | None = None,
    agentmail_send_func: Any | None = None,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    item = state.get_dead_letter(dead_letter_id)
    if not item:
        return _blocked("dead_letter_not_found", dead_letter_id=dead_letter_id)
    if not live:
        return _blocked("live_flag_required", dead_letter_id=dead_letter_id)
    return _process_incident(
        item,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
        channel_id=channel_id,
        fallback_email=fallback_email,
        notification_dry_run=notification_dry_run,
        post_func=post_func,
        agentmail_send_func=agentmail_send_func,
        write_audit=True,
        now_local=now_local,
    )


def respond_to_incident(
    *,
    dead_letter_id: str,
    action: str,
    live: bool = False,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    if action not in RESPOND_ACTIONS:
        return _blocked("unsupported_incident_response", action=action)
    if not live:
        return _blocked("live_flag_required", action=action)
    state = AssistantSchedulerState(scheduler_db_path)
    before = state.get_dead_letter(dead_letter_id)
    if not before:
        return _blocked("dead_letter_not_found", dead_letter_id=dead_letter_id)
    after = state.update_dead_letter_status(dead_letter_id, RESPOND_ACTIONS[action])
    event = AssistantAuditLog(ledger_path).record_event(
        event_type="assistant.incident_recovered" if action == "recover" else "scheduler.dead_letter_resolved",
        workflow=WORKFLOW,
        idempotency_key=str((after or before).get("idempotency_key") or dead_letter_id),
        policy_version=POLICY_VERSION,
        decision="recovered" if action == "recover" else "observed",
        reason=f"assistant_incident_{RESPOND_ACTIONS[action]}",
        sources=[dead_letter_id],
        artifacts={"before": _safe_dead_letter(before), "after": _safe_dead_letter(after or {})},
    )
    return redact_payload({"status": RESPOND_ACTIONS[action], "reason": f"incident_{RESPOND_ACTIONS[action]}", "dead_letter": _safe_dead_letter(after or {}), "audit_id": event.audit_id, "state_mutated": True, "calendar_write_attempted": False, "notion_write_attempted": False})


def list_incidents(*, limit: int = 20, scheduler_db_path: str | Path | None = None) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    rows = [item for item in state.list_dead_letters(status=None, limit=limit) if item.get("status") in INCIDENT_STATUSES]
    return redact_payload({
        "status": "ok",
        "count": len(rows),
        "incidents": [_safe_dead_letter(item) for item in rows],
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def _process_incident(
    item: dict[str, Any],
    *,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    channel_id: str,
    fallback_email: str,
    notification_dry_run: bool,
    post_func: Any | None,
    agentmail_send_func: Any | None,
    write_audit: bool,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    dead_letter_id = str(item.get("dead_letter_id"))
    failure_class = str(item.get("failure_class") or "")
    job_name = str(item.get("job_name") or "")
    state.update_dead_letter_status(dead_letter_id, "retrying")
    if failure_class == "daily_personal_briefing_notification_failed":
        result = _resend_daily(item, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, channel_id=channel_id, fallback_email=fallback_email, notification_dry_run=notification_dry_run, post_func=post_func, agentmail_send_func=agentmail_send_func)
        return _finalize_after_notification(item, result, recovered_on_success=True, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)
    if failure_class == "weekly_personal_review_notification_failed":
        result = _resend_weekly(item, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, channel_id=channel_id, fallback_email=fallback_email, notification_dry_run=notification_dry_run, post_func=post_func, agentmail_send_func=agentmail_send_func)
        return _finalize_after_notification(item, result, recovered_on_success=True, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)
    if job_name == "assistant_notification_dispatcher":
        result = dispatch_failure_notification(
            {"workflow": job_name, "status": "manual_review_required", "reason": failure_class, "idempotency_key": item.get("idempotency_key")},
            channel_id=channel_id,
            fallback_email=fallback_email,
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            dry_run=notification_dry_run,
            post_func=post_func,
            agentmail_send_func=agentmail_send_func,
        )
        return _finalize_after_notification(item, result, recovered_on_success=True, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)
    if job_name == "email_triage_booking" and failure_class == "no_available_slot":
        result = _notify_email_triage_no_slot(item, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, channel_id=channel_id, fallback_email=fallback_email, notification_dry_run=notification_dry_run, post_func=post_func, agentmail_send_func=agentmail_send_func, now_local=now_local)
        status = "waiting_for_user" if result.get("status") in {"posted", "dry_run"} else "open"
        state.update_dead_letter_status(dead_letter_id, status)
        _audit_incident("assistant.incident_waiting_for_user", item, result, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, decision="observed", write_audit=write_audit)
        return _incident_summary(state.get_dead_letter(dead_letter_id) or item, action="asked_for_guidance", notification=result)
    if job_name == "email_triage_booking" and failure_class == "email_triage_proposal_not_found":
        result = _notify_email_triage_proposal_not_found(item, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, channel_id=channel_id, fallback_email=fallback_email, notification_dry_run=notification_dry_run, post_func=post_func, agentmail_send_func=agentmail_send_func, now_local=now_local)
        status = "waiting_for_user" if result.get("status") in {"posted", "dry_run"} else "open"
        state.update_dead_letter_status(dead_letter_id, status)
        _audit_incident("assistant.incident_waiting_for_user", item, result, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, decision="observed", write_audit=write_audit)
        return _incident_summary(state.get_dead_letter(dead_letter_id) or item, action="asked_for_guidance", notification=result)
    if job_name == "email_triage_booking" and failure_class == "launchagent_nonzero_exit":
        if _has_related_email_no_slot_guidance(state):
            after = state.update_dead_letter_status(dead_letter_id, "recovered")
            _audit_incident("assistant.incident_recovered", item, {"status": "recovered", "reason": "covered_by_no_slot_guidance"}, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, decision="recovered", write_audit=write_audit)
            return _incident_summary(after or item, action="recovered_as_business_outcome")
    result = dispatch_failure_notification(
        {"workflow": job_name, "status": "manual_review_required", "reason": failure_class, "idempotency_key": item.get("idempotency_key")},
        channel_id=channel_id,
        fallback_email=fallback_email,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        dry_run=notification_dry_run,
        post_func=post_func,
        agentmail_send_func=agentmail_send_func,
    )
    return _finalize_after_notification(item, result, recovered_on_success=False, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _incident_priority(item: dict[str, Any]) -> tuple[int, str]:
    failure_class = str(item.get("failure_class") or "")
    job_name = str(item.get("job_name") or "")
    if job_name == "email_triage_booking" and failure_class == "no_available_slot":
        return (0, str(item.get("updated_at") or ""))
    if job_name == "email_triage_booking" and failure_class == "launchagent_nonzero_exit":
        return (1, str(item.get("updated_at") or ""))
    if failure_class in {"daily_personal_briefing_notification_failed", "weekly_personal_review_notification_failed", "assistant_notification_failed"}:
        return (2, str(item.get("updated_at") or ""))
    return (3, str(item.get("updated_at") or ""))


def _resend_daily(item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    target_date = _date_from_item(item)
    briefing = run_daily_personal_briefing(planning_date=target_date, scheduler_db_path=kwargs.get("scheduler_db_path"))
    return dispatch_user_notification(
        workflow="daily_personal_briefing",
        message=str(briefing.get("discord_message") or "Rocky daily brief is empty."),
        subject=f"Rocky daily brief - {target_date}",
        reason="daily_personal_briefing_resend",
        target_date=target_date,
        idempotency_key=str(item.get("idempotency_key") or ""),
        channel_id=kwargs["channel_id"],
        fallback_email=kwargs["fallback_email"],
        ledger_path=kwargs.get("ledger_path"),
        scheduler_db_path=kwargs.get("scheduler_db_path"),
        dry_run=kwargs["notification_dry_run"],
        post_func=kwargs.get("post_func"),
        agentmail_send_func=kwargs.get("agentmail_send_func"),
    )


def _resend_weekly(item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    target_date = _date_from_item(item)
    review = build_weekly_personal_review(planning_date=target_date, scheduler_db_path=kwargs.get("scheduler_db_path"))
    return dispatch_user_notification(
        workflow="weekly_personal_review",
        message=str(review.get("discord_message") or "Rocky weekly review is empty."),
        subject=f"Rocky weekly review - {review.get('week_label') or target_date}",
        reason="weekly_personal_review_resend",
        target_date=target_date,
        idempotency_key=str(item.get("idempotency_key") or ""),
        channel_id=kwargs["channel_id"],
        fallback_email=kwargs["fallback_email"],
        ledger_path=kwargs.get("ledger_path"),
        scheduler_db_path=kwargs.get("scheduler_db_path"),
        dry_run=kwargs["notification_dry_run"],
        post_func=kwargs.get("post_func"),
        agentmail_send_func=kwargs.get("agentmail_send_func"),
    )


def _notify_email_triage_no_slot(item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    today = _local_today(kwargs.get("now_local"))
    target_date = _date_from_item(item, default_date=today)
    if target_date < today:
        heading = f"Rocky could not book email triage for {target_date}."
        date_context = f"Today is {today}; this is an unresolved incident from the earlier run."
        option_one = "1. Let Rocky book catch-up email triage in the smallest safe chunks today (15-30 minutes) if possible."
        option_three = f"3. Skip/acknowledge the {target_date} email triage incident."
    elif target_date == today:
        heading = f"Rocky could not book email triage for today ({today})."
        date_context = "This is today's email triage booking incident."
        option_one = "1. Let Rocky split email triage into the smallest safe chunks today (15-30 minutes)."
        option_three = "3. Skip email triage for today."
    else:
        heading = f"Rocky could not book email triage for {target_date}."
        date_context = f"Today is {today}; this incident targets a future date."
        option_one = "1. Let Rocky split email triage into the smallest safe chunks on the target date (15-30 minutes)."
        option_three = f"3. Skip/acknowledge the {target_date} email triage incident."
    message = "\n".join(
        [
            heading,
            date_context,
            "",
            "Reason: the calendar had no safe same-day slot for the estimated email triage block.",
            "Note: the 60-minute minimum applies only to coding focus blocks, not email triage.",
            "",
            "Please choose one:",
            option_one,
            "2. Book the next allowed Monday-Thursday slot.",
            option_three,
            "",
            f"Incident: {item.get('dead_letter_id')}",
        ]
    )
    return dispatch_user_notification(
        workflow="email_triage_scheduler",
        message=message,
        subject=f"Rocky needs guidance: email triage no slot - {target_date}",
        reason="email_triage_no_available_slot_guidance_needed",
        target_date=target_date,
        idempotency_key=str(item.get("idempotency_key") or ""),
        channel_id=kwargs["channel_id"],
        fallback_email=kwargs["fallback_email"],
        ledger_path=kwargs.get("ledger_path"),
        scheduler_db_path=kwargs.get("scheduler_db_path"),
        dry_run=kwargs["notification_dry_run"],
        post_func=kwargs.get("post_func"),
        agentmail_send_func=kwargs.get("agentmail_send_func"),
    )


def _notify_email_triage_proposal_not_found(item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    today = _local_today(kwargs.get("now_local"))
    target_date = _date_from_item(item, default_date=today)
    if target_date == today:
        heading = f"Rocky could not finalize email triage for today ({today})."
        retry_line = "1. Let Rocky retry email triage now using the current unread-mail signal and 15-30 minute chunks."
        skip_line = "3. Skip email triage for today."
    elif target_date < today:
        heading = f"Rocky could not finalize email triage for {target_date}."
        retry_line = f"1. Let Rocky treat this as catch-up triage today ({today}) using 15-30 minute chunks."
        skip_line = f"3. Skip/acknowledge the {target_date} email triage incident."
    else:
        heading = f"Rocky could not finalize email triage for {target_date}."
        retry_line = "1. Let Rocky retry on the target date using the current unread-mail signal and 15-30 minute chunks."
        skip_line = f"3. Skip/acknowledge the {target_date} email triage incident."
    message = "\n".join(
        [
            heading,
            f"Today is {today}.",
            "",
            "Reason: the unread-mail proposal changed between planning and live booking, so Rocky refused to book a stale calendar block.",
            "What Rocky did: stopped before writing Calendar and recorded the incident.",
            "What changed: future runs now pass the approved proposal snapshot into booking so this specific mismatch should not recur.",
            "",
            "Please choose one:",
            retry_line,
            "2. Book the next allowed Monday-Thursday slot.",
            skip_line,
            "",
            f"Incident: {item.get('dead_letter_id')}",
        ]
    )
    return dispatch_user_notification(
        workflow="email_triage_scheduler",
        message=message,
        subject=f"Rocky needs guidance: email triage proposal changed - {target_date}",
        reason="email_triage_proposal_changed_guidance_needed",
        target_date=target_date,
        idempotency_key=str(item.get("idempotency_key") or ""),
        channel_id=kwargs["channel_id"],
        fallback_email=kwargs["fallback_email"],
        ledger_path=kwargs.get("ledger_path"),
        scheduler_db_path=kwargs.get("scheduler_db_path"),
        dry_run=kwargs["notification_dry_run"],
        post_func=kwargs.get("post_func"),
        agentmail_send_func=kwargs.get("agentmail_send_func"),
    )


def _finalize_after_notification(
    item: dict[str, Any],
    notification: dict[str, Any],
    *,
    recovered_on_success: bool,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    write_audit: bool,
) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    dead_letter_id = str(item.get("dead_letter_id"))
    if notification.get("status") in {"posted", "dry_run"}:
        next_status = "recovered" if recovered_on_success else "notified"
    else:
        next_status = "open"
    after = state.update_dead_letter_status(dead_letter_id, next_status)
    event_type = "assistant.incident_recovered" if next_status == "recovered" else "assistant.incident_notified"
    _audit_incident(event_type, item, notification, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, decision="recovered" if next_status == "recovered" else "observed", write_audit=write_audit)
    return _incident_summary(after or item, action=f"marked_{next_status}", notification=notification)


def _audit_incident(
    event_type: str,
    item: dict[str, Any],
    notification: dict[str, Any],
    *,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    decision: str,
    write_audit: bool,
) -> None:
    if not write_audit:
        return
    AssistantAuditLog(ledger_path).record_event(
        event_type=event_type,
        workflow=WORKFLOW,
        idempotency_key=str(item.get("idempotency_key") or item.get("dead_letter_id")),
        policy_version=POLICY_VERSION,
        decision=decision,
        reason=str(notification.get("reason") or notification.get("status")),
        sources=[str(item.get("dead_letter_id"))],
        artifacts={"dead_letter": _safe_dead_letter(item), "notification": notification},
    )


def _should_process(item: dict[str, Any], *, quiet_minutes: int) -> bool:
    if item.get("status") == "open":
        return True
    updated = _parse_dt(item.get("updated_at"))
    if not updated:
        return True
    age_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
    return age_seconds >= quiet_minutes * 60


def _has_related_email_no_slot_guidance(state: AssistantSchedulerState) -> bool:
    for item in state.list_dead_letters(status=None, limit=100):
        if item.get("job_name") == "email_triage_booking" and item.get("failure_class") == "no_available_slot" and item.get("status") in {"waiting_for_user", "recovered", "acknowledged"}:
            return True
    return False


def _date_from_item(item: dict[str, Any], *, default_date: str | None = None) -> str:
    for value in [item.get("idempotency_key"), item.get("safe_summary"), item.get("recovery_hint")]:
        match = re.search(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", str(value or ""))
        if match:
            return match.group(0)
    return default_date or _local_today(None)


def _local_today(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        parsed = value.astimezone(ZoneInfo(TIMEZONE)) if value.tzinfo else value.replace(tzinfo=ZoneInfo(TIMEZONE))
        return parsed.date().isoformat()
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed = parsed.astimezone(ZoneInfo(TIMEZONE)) if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
        return parsed.date().isoformat()
    return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _incident_summary(item: dict[str, Any], *, action: str, notification: dict[str, Any] | None = None) -> dict[str, Any]:
    return redact_payload({
        "dead_letter_id": item.get("dead_letter_id"),
        "job_name": item.get("job_name"),
        "failure_class": item.get("failure_class"),
        "status": item.get("status"),
        "action": action,
        "notification": _safe_notification(notification or {}),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["dead_letter_id", "job_name", "workflow", "idempotency_key", "failure_class", "safe_summary", "recovery_hint", "attempts", "status", "last_failed_at", "updated_at", "error_hash"]}


def _safe_notification(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["status", "reason", "final_status", "fallback_used", "message_sha256", "primary_failure_reason", "audit_id"] if key in item}


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return redact_payload({"status": "blocked", "reason": reason, **extra, "calendar_write_attempted": False, "notion_write_attempted": False, "state_mutated": False})


def _write_state(path: str | Path | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(redact_payload({"last_run_at": utc_now_iso(), "last_status": payload.get("status"), "processed_count": payload.get("processed_count"), "reason": payload.get("reason")}), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky assistant incident manager.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--channel-id", default=DEFAULT_ALERT_CHANNEL_ID, dest="channel_id")
    parser.add_argument("--fallback-email", default=DEFAULT_FALLBACK_EMAIL, dest="fallback_email")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--quiet-minutes", type=int, default=240, dest="quiet_minutes")
    parser.add_argument("--no-write-audit", action="store_false", default=True, dest="write_audit")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_incident_manager(
        live=args.live,
        limit=args.limit,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        channel_id=args.channel_id,
        fallback_email=args.fallback_email,
        notification_dry_run=args.notification_dry_run,
        quiet_minutes=args.quiet_minutes,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Incident manager: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "manual_review_required", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
