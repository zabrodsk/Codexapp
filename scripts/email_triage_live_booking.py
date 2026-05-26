#!/usr/bin/env python3
"""Supervised live booking for Rocky email triage blocks."""
from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from assistant_calendar_status import calendar_write_health
from assistant_calendar_writer import create_calendar_block
from email_triage_proposal_engine import BOOKING_REASON, TIMEZONE, build_email_triage_proposals


def book_email_triage_proposal(
    *,
    idempotency_key: str,
    planning_date: str | None = None,
    calendar_name: str = "Calendar",
    live: bool = False,
    hours: int = 168,
    limit: int = 100,
    db_path: str | Path | None = None,
    state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    helper_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    proposal_payload: dict[str, Any] | None = None,
    health_payload: dict[str, Any] | None = None,
    now_local: str | datetime | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    if not live:
        return _blocked(idempotency_key=idempotency_key, reason="live_flag_required", mode="dry_run")
    planning_day = _parse_planning_day(planning_date, now)
    if planning_day != now.date():
        return _blocked(idempotency_key=idempotency_key, reason="email_triage_must_be_same_day")
    if now.time() < time(8, 0):
        return _blocked(idempotency_key=idempotency_key, reason="email_triage_not_before_morning")

    proposals = proposal_payload or build_email_triage_proposals(
        planning_date=planning_day,
        hours=hours,
        limit=limit,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=False,
        helper_payload=helper_payload,
        existing_events=existing_events,
        now_local=now,
    )
    selected = _find_selected(proposals, idempotency_key)
    if selected is None:
        return _blocked(
            idempotency_key=idempotency_key,
            reason="email_triage_proposal_not_found",
            proposal_payload=_safe_proposal_payload(proposals),
            available_idempotency_keys=[
                item.get("idempotency_key")
                for item in proposals.get("proposals", [])
                if item.get("idempotency_key")
            ],
        )
    if selected.get("status") == "skipped_duplicate":
        return {
            "status": "skipped_duplicate",
            "reason": "duplicate_rocky_block",
            "mode": "preflight",
            "idempotency_key": idempotency_key,
            "calendar_name": calendar_name,
            "selected_proposal": _safe_selected(selected),
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }
    if selected.get("status") != "proposal":
        return _blocked(
            idempotency_key=idempotency_key,
            reason=str(selected.get("reason") or "email_triage_proposal_not_bookable"),
            selected_proposal=_safe_selected(selected),
        )

    health = health_payload
    if health is None:
        health = calendar_write_health(db_path=db_path, ledger_path=ledger_path, write_audit=False)
    if health.get("status") != "ok":
        return _blocked(
            idempotency_key=idempotency_key,
            reason="calendar_write_health_not_ok",
            health={"status": health.get("status"), "blocked_checks": list(health.get("blocked_checks") or [])},
        )

    proposal = selected.get("proposal") or {}
    start_hhmm = _iso_to_hhmm(str(proposal.get("start")))
    duration = int(selected.get("duration_minutes") or proposal.get("duration_minutes") or 30)
    attention = selected.get("email_attention") or {}
    estimate = selected.get("estimate") or {}
    result = create_calendar_block(
        kind="email_triage",
        day=planning_day.isoformat(),
        window_start=start_hhmm,
        window_end=_iso_to_hhmm(str(proposal.get("end"))),
        duration_minutes=duration,
        reason=BOOKING_REASON,
        confidence=str(estimate.get("confidence") or "medium"),
        source_refs=[f"email-triage:{attention.get('evidence_hash') or idempotency_key}"],
        metadata_extra={
            "unread_count": int(attention.get("unread_count") or 0),
            "attention_count": int(attention.get("attention_count") or 0),
            "priority_buckets": str(attention.get("priority_buckets") or {}),
            "estimated_minutes": int(estimate.get("estimated_minutes") or duration),
        },
        calendar_name=calendar_name,
        live=True,
        state_db_path=state_db_path,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
        db_path=db_path,
        existing_events=existing_events,
    )
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "mode": "live" if result.get("calendar_write_attempted") else "preflight",
        "idempotency_key": idempotency_key,
        "calendar_name": calendar_name,
        "selected_proposal": _safe_selected(selected),
        "calendar_result": _safe_calendar_result(result),
        "audit_id": result.get("audit_id"),
        "calendar_write_attempted": bool(result.get("calendar_write_attempted")),
        "calendar_event_created": bool(result.get("calendar_event_created")),
        "calendar_event_deleted": bool(result.get("calendar_event_deleted")),
    }


def _blocked(
    *,
    idempotency_key: str,
    reason: str,
    mode: str = "preflight",
    health: dict[str, Any] | None = None,
    proposal_payload: dict[str, Any] | None = None,
    selected_proposal: dict[str, Any] | None = None,
    available_idempotency_keys: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "blocked",
        "reason": reason,
        "mode": mode,
        "idempotency_key": idempotency_key,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
    }
    if health is not None:
        payload["health"] = health
    if proposal_payload is not None:
        payload["proposal_payload"] = proposal_payload
    if selected_proposal is not None:
        payload["selected_proposal"] = selected_proposal
    if available_idempotency_keys is not None:
        payload["available_idempotency_keys"] = available_idempotency_keys
    return payload


def _find_selected(payload: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
    for proposal in payload.get("proposals") or []:
        if proposal.get("idempotency_key") == idempotency_key:
            return proposal
    return None


def _safe_proposal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "planning_date": payload.get("planning_date"),
        "target_date": payload.get("target_date"),
        "calendar_write_attempted": payload.get("calendar_write_attempted"),
        "proposal_count": len(payload.get("proposals") or []),
    }


def _safe_selected(selected: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": selected.get("status"),
        "reason": selected.get("reason"),
        "idempotency_key": selected.get("idempotency_key"),
        "audit_id": selected.get("audit_id"),
        "proposal": selected.get("proposal"),
        "email_attention": selected.get("email_attention"),
        "estimate": selected.get("estimate"),
        "calendar_write_attempted": False,
    }


def _safe_calendar_result(result: dict[str, Any]) -> dict[str, Any]:
    safe = dict(result)
    if "metadata_description" in safe:
        safe["metadata_description"] = {
            "redacted": True,
            "reason": "calendar_metadata_not_repeated_in_email_booking_output",
        }
    return safe


def _parse_now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(TIMEZONE)) if value.tzinfo else value.replace(tzinfo=ZoneInfo(TIMEZONE))
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo(TIMEZONE)) if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return datetime.now(ZoneInfo(TIMEZONE))


def _parse_planning_day(value: str | None, now: datetime):
    if not value:
        return now.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _iso_to_hhmm(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return parsed.strftime("%H:%M")
