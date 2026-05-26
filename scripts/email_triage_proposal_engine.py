#!/usr/bin/env python3
"""Dry-run email triage calendar proposal engine."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_calendar_dry_run import build_calendar_dry_run
from assistant_calendar_policy import evaluate_calendar_policy
from email_triage_reader import DEFAULT_HOURS, DEFAULT_LIMIT, collect_email_attention
from email_triage_time_estimator import estimate_email_triage_minutes


TIMEZONE = "Europe/Prague"
PREFERRED_WINDOW_START = "13:00"
PREFERRED_WINDOW_END = "15:30"
FALLBACK_WINDOW_START = "12:00"
FALLBACK_WINDOW_END = "17:30"
BOOKING_REASON = "Unread Inbox emails requiring manual attention"
SPLIT_RECOVERY_DURATIONS_MINUTES = (30, 15)


def build_email_triage_proposals(
    *,
    planning_date: str | date | None = None,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
    helper_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    base = {
        "mode": "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": planning_day.isoformat(),
        "timezone": TIMEZONE,
        "calendar_write_attempted": False,
        "proposals": [],
    }
    if planning_day != now.date():
        return {
            **base,
            "status": "blocked",
            "reason": "email_triage_must_be_same_day",
        }
    if planning_day.weekday() >= 4:
        return {
            **base,
            "status": "blocked",
            "reason": "proactive_booking_blocked_on_friday_saturday_sunday",
        }

    attention = collect_email_attention(hours=hours, limit=limit, helper_payload=helper_payload)
    if attention.get("status") != "ok":
        return {
            **base,
            "status": "blocked",
            "reason": str(attention.get("reason") or "email_attention_read_failed"),
            "email_attention": _safe_attention_summary(attention),
        }

    estimate = estimate_email_triage_minutes(attention)
    if int(attention.get("attention_count") or 0) <= 0:
        return {
            **base,
            "status": "skipped_no_attention_emails",
            "reason": "no_unread_attention_emails",
            "email_attention": _safe_attention_summary(attention),
            "estimate": estimate,
            "skipped_count": 1,
        }

    day_events = existing_events
    if day_events is None:
        day_events = query_events(
            db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
            start=datetime.combine(planning_day, time(0, 0)),
            end=datetime.combine(planning_day, time(23, 59, 59)),
            include_all_day=False,
        )
    duplicate = _same_day_email_duplicate(day_events, planning_day)
    if duplicate:
        decision = evaluate_calendar_policy(
            kind="email_triage",
            day=planning_day,
            start=PREFERRED_WINDOW_START,
            duration_minutes=int(estimate["estimated_minutes"]),
            source_refs=[_attention_source_ref(attention)],
        )
        proposal = {
            "status": "skipped_duplicate",
            "reason": "duplicate_rocky_block",
            "idempotency_key": decision.idempotency_key,
            "calendar_write_attempted": False,
            "duplicate_count": 1,
            "policy_decision": decision.to_dict(),
            "email_attention": _safe_attention_summary(attention),
            "estimate": estimate,
        }
        return {
            **base,
            "status": "skipped_duplicate",
            "reason": "duplicate_rocky_block",
            "email_attention": _safe_attention_summary(attention),
            "estimate": estimate,
            "proposals": [proposal],
            "selected_proposal": proposal,
            "idempotency_key": decision.idempotency_key,
            "skipped_count": 1,
        }

    proposal = _build_window_proposal(
        planning_day=planning_day,
        window_start=PREFERRED_WINDOW_START,
        window_end=PREFERRED_WINDOW_END,
        duration_minutes=int(estimate["estimated_minutes"]),
        attention=attention,
        estimate=estimate,
        db_path=db_path,
        existing_events=day_events,
        ledger_path=ledger_path,
        write_audit=write_audit,
    )
    proposals = [proposal]
    if proposal.get("reason") == "no_available_slot":
        fallback = _build_window_proposal(
            planning_day=planning_day,
            window_start=FALLBACK_WINDOW_START,
            window_end=FALLBACK_WINDOW_END,
            duration_minutes=int(estimate["estimated_minutes"]),
            attention=attention,
            estimate=estimate,
            db_path=db_path,
            existing_events=day_events,
            ledger_path=ledger_path,
            write_audit=write_audit,
        )
        proposals.append(fallback)
        proposal = fallback
    if proposal.get("reason") == "no_available_slot":
        original_duration = int(estimate["estimated_minutes"])
        tried_durations = {original_duration}
        for split_duration in SPLIT_RECOVERY_DURATIONS_MINUTES:
            if split_duration >= original_duration or split_duration in tried_durations:
                continue
            tried_durations.add(split_duration)
            partial = _build_window_proposal(
                planning_day=planning_day,
                window_start=FALLBACK_WINDOW_START,
                window_end=FALLBACK_WINDOW_END,
                duration_minutes=split_duration,
                attention=attention,
                estimate=estimate,
                db_path=db_path,
                existing_events=day_events,
                ledger_path=ledger_path,
                write_audit=write_audit,
            )
            partial["split_recovery"] = True
            partial["original_duration_minutes"] = original_duration
            proposals.append(partial)
            proposal = partial
            if partial.get("status") == "proposal":
                break

    status = "proposal" if proposal.get("status") == "proposal" else "blocked"
    return {
        **base,
        "status": status,
        "reason": None if status == "proposal" else str(proposal.get("reason") or "email_triage_proposal_blocked"),
        "email_attention": _safe_attention_summary(attention),
        "estimate": estimate,
        "proposals": proposals,
        "selected_proposal": proposal,
        "idempotency_key": proposal.get("idempotency_key"),
        "audit_id": proposal.get("audit_id"),
        "blocked_count": 1 if status == "blocked" else 0,
    }


def _build_window_proposal(
    *,
    planning_day: date,
    window_start: str,
    window_end: str,
    duration_minutes: int,
    attention: dict[str, Any],
    estimate: dict[str, Any],
    db_path: str | Path | None,
    existing_events: list[dict[str, Any]],
    ledger_path: str | Path | None,
    write_audit: bool,
) -> dict[str, Any]:
    metadata = _metadata_extra(attention, estimate)
    proposal = build_calendar_dry_run(
        kind="email_triage",
        day=planning_day.isoformat(),
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        reason=BOOKING_REASON,
        source_refs=[_attention_source_ref(attention)],
        confidence=str(estimate.get("confidence") or "medium"),
        metadata_extra=metadata,
        db_path=db_path,
        existing_events=existing_events,
        ledger_path=ledger_path,
        record_audit=write_audit,
    )
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "idempotency_key": proposal.get("idempotency_key"),
        "audit_id": proposal.get("audit_id"),
        "audit_event_ids": proposal.get("audit_event_ids") or [],
        "proposal": _safe_calendar_proposal(proposal),
        "window_start": window_start,
        "window_end": window_end,
        "duration_minutes": duration_minutes,
        "email_attention": _safe_attention_summary(attention),
        "estimate": estimate,
        "calendar_write_attempted": False,
    }


def _metadata_extra(attention: dict[str, Any], estimate: dict[str, Any]) -> dict[str, Any]:
    return {
        "unread_count": int(attention.get("unread_count") or 0),
        "attention_count": int(attention.get("attention_count") or 0),
        "priority_buckets": json.dumps(attention.get("priority_buckets") or {}, sort_keys=True),
        "estimated_minutes": int(estimate.get("estimated_minutes") or 0),
    }


def _same_day_email_duplicate(events: list[dict[str, Any]], planning_day: date) -> dict[str, Any] | None:
    day_prefix = planning_day.isoformat()
    for event in events:
        if event.get("all_day"):
            continue
        if not str(event.get("start_local") or "").startswith(day_prefix):
            continue
        if str(event.get("summary") or "").startswith("Rocky: Email triage"):
            return event
    return None


def _safe_calendar_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "title": proposal.get("title"),
        "start": proposal.get("start"),
        "end": proposal.get("end"),
        "duration_minutes": proposal.get("duration_minutes"),
        "confidence": proposal.get("confidence"),
        "calendar_write_attempted": bool(proposal.get("calendar_write_attempted")),
    }


def _safe_attention_summary(attention: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": attention.get("status"),
        "source": attention.get("source"),
        "hours": attention.get("hours"),
        "limit": attention.get("limit"),
        "unread_count": attention.get("unread_count", 0),
        "evaluated_count": attention.get("evaluated_count", 0),
        "attention_count": attention.get("attention_count", 0),
        "priority_buckets": attention.get("priority_buckets") or {},
        "source_refs": list(attention.get("source_refs") or []),
        "evidence_hash": attention.get("evidence_hash"),
        "error_hash": attention.get("error_hash"),
    }


def _attention_source_ref(attention: dict[str, Any]) -> str:
    evidence = str(attention.get("evidence_hash") or _hash_json(_safe_attention_summary(attention)))
    return f"email-triage:{evidence}"


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


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
