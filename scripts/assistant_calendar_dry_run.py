#!/usr/bin/env python3
"""Dry-run calendar proposal builder for Rocky.

This module never writes to Apple Calendar. It reads existing events, applies
Rocky's proactive booking policy, and records audit events for traceability.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_audit_log import AssistantAuditLog
from assistant_calendar_policy import (
    POLICY_VERSION,
    TITLE_PREFIX_BY_KIND,
    combine_local,
    evaluate_calendar_policy,
    stable_idempotency_key,
    title_for_kind,
)


def _parse_event_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and end_a > start_b


def _event_interval(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    if event.get("all_day"):
        return None
    try:
        return _parse_event_dt(str(event["start_local"])), _parse_event_dt(str(event["end_local"]))
    except Exception:
        return None


def _event_signature(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_hash": _safe_hash(event.get("summary")),
        "start_local": event.get("start_local"),
        "end_local": event.get("end_local"),
        "calendar": event.get("calendar"),
    }


def _safe_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _is_matching_rocky_duplicate(
    event: dict[str, Any],
    *,
    kind: str,
    title: str,
    idempotency_key: str,
) -> bool:
    summary = str(event.get("summary") or "")
    description = str(event.get("description") or "")
    prefix = TITLE_PREFIX_BY_KIND.get(kind, "Rocky:")
    return (
        idempotency_key in description
        or summary == title
        or (summary.startswith(prefix) and title.startswith(prefix))
    )


def _blocking_events(
    events: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for event in events:
        interval = _event_interval(event)
        if interval is None:
            continue
        event_start, event_end = interval
        if _overlaps(start.replace(tzinfo=None), end.replace(tzinfo=None), event_start, event_end):
            blockers.append(event)
    return blockers


def _find_earliest_slot(
    events: list[dict[str, Any]],
    *,
    day: str,
    window_start: str,
    window_end: str,
    duration_minutes: int,
) -> tuple[datetime | None, datetime | None, list[dict[str, Any]]]:
    candidate_start = combine_local(day, window_start)
    window_end_dt = combine_local(day, window_end)
    duration = timedelta(minutes=duration_minutes)
    all_blockers: list[dict[str, Any]] = []

    while candidate_start + duration <= window_end_dt:
        candidate_end = candidate_start + duration
        blockers = _blocking_events(events, start=candidate_start, end=candidate_end)
        if not blockers:
            return candidate_start, candidate_end, all_blockers
        all_blockers.extend(blockers)
        latest_blocker_end = max(_event_interval(event)[1] for event in blockers if _event_interval(event))
        candidate_start = latest_blocker_end.replace(tzinfo=candidate_start.tzinfo)

    return None, None, all_blockers


def build_metadata_description(
    *,
    kind: str,
    reason: str,
    sources: list[Any],
    confidence: str,
    audit_id: str,
    idempotency_key: str,
    created_at: str,
    metadata_extra: dict[str, Any] | None = None,
) -> str:
    safe_sources = ", ".join(str(item) for item in sources) if sources else "none"
    lines = [
        "Booked by: Rocky",
        f"Block type: {kind}",
        f"Reason: {reason}",
    ]
    for key, value in (metadata_extra or {}).items():
        label = " ".join(str(key).replace("_", " ").split()).title()
        if isinstance(value, (dict, list, tuple)):
            rendered = str(value)
        else:
            rendered = str(value)
        lines.append(f"{label}: {rendered}")
    lines.extend(
        [
            f"Sources: {safe_sources}",
            f"Confidence: {confidence}",
            f"Audit ID: {audit_id}",
            f"Idempotency key: {idempotency_key}",
            f"Created at: {created_at}",
            "Reversal instruction: Delete this Rocky-owned calendar block if it is no longer useful.",
        ]
    )
    return "\n".join(lines)


def build_calendar_dry_run(
    *,
    kind: str,
    day: str,
    window_start: str,
    window_end: str,
    duration_minutes: int,
    label: str | None = None,
    reason: str = "Rocky dry-run calendar proposal",
    source_refs: list[Any] | None = None,
    confidence: str = "medium",
    metadata_extra: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    ledger_path: Path | str | None = None,
    record_audit: bool = True,
) -> dict[str, Any]:
    sources = list(source_refs or [])
    preliminary = evaluate_calendar_policy(
        kind=kind,
        day=day,
        start=window_start,
        duration_minutes=duration_minutes,
        source_refs=sources,
        label=label,
    )
    audit_log = AssistantAuditLog(ledger_path) if record_audit else None
    audit_event_ids: list[str] = []

    if not preliminary.allowed:
        event = None
        if audit_log:
            event = audit_log.record_event(
                event_type="policy.violation",
                workflow="calendar_dry_run",
                idempotency_key=preliminary.idempotency_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason=",".join(preliminary.reasons),
                sources=sources,
                artifacts={"policy_decision": preliminary.to_dict()},
            )
            audit_event_ids.append(event.audit_id)
            blocked_event = audit_log.record_event(
                event_type="calendar.proposal_blocked",
                workflow="calendar_dry_run",
                idempotency_key=preliminary.idempotency_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason="policy_blocked",
                sources=sources,
                artifacts={"policy_decision": preliminary.to_dict()},
            )
            audit_event_ids.append(blocked_event.audit_id)
        return {
            "status": "blocked",
            "reason": "policy_blocked",
            "audit_id": event.audit_id if event else None,
            "audit_event_ids": audit_event_ids,
            "idempotency_key": preliminary.idempotency_key,
            "policy_decision": preliminary.to_dict(),
            "calendar_write_attempted": False,
        }

    if existing_events is None:
        db = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH
        existing_events = query_events(
            db_path=db,
            start=combine_local(day, window_start).replace(tzinfo=None),
            end=combine_local(day, window_end).replace(tzinfo=None),
            include_all_day=False,
        )

    planned_title = title_for_kind(kind, label=label)
    window_duplicates = [
        event
        for event in _blocking_events(
            existing_events,
            start=combine_local(day, window_start),
            end=combine_local(day, window_end),
        )
        if _is_matching_rocky_duplicate(
            event,
            kind=kind,
            title=planned_title,
            idempotency_key=preliminary.idempotency_key,
        )
    ]
    if window_duplicates:
        if audit_log:
            blocked_event = audit_log.record_event(
                event_type="calendar.proposal_blocked",
                workflow="calendar_dry_run",
                idempotency_key=preliminary.idempotency_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason="duplicate_rocky_block",
                sources=sources,
                artifacts={
                    "duplicate_count": len(window_duplicates),
                    "duplicate_signatures": [_event_signature(event) for event in window_duplicates],
                },
            )
            audit_event_ids.append(blocked_event.audit_id)
        return {
            "status": "blocked",
            "reason": "duplicate_rocky_block",
            "audit_id": audit_event_ids[-1] if audit_event_ids else None,
            "audit_event_ids": audit_event_ids,
            "idempotency_key": preliminary.idempotency_key,
            "calendar_write_attempted": False,
            "duplicate_count": len(window_duplicates),
            "policy_decision": preliminary.to_dict(),
        }

    slot_start, slot_end, blockers_seen = _find_earliest_slot(
        existing_events,
        day=day,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
    )
    if slot_start is None or slot_end is None:
        if audit_log:
            conflict_event = audit_log.record_event(
                event_type="calendar.conflict_detected",
                workflow="calendar_dry_run",
                idempotency_key=preliminary.idempotency_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason="no_available_slot",
                sources=sources,
                artifacts={
                    "conflict_count": len(blockers_seen),
                    "conflict_signatures": [_event_signature(event) for event in blockers_seen],
                },
            )
            audit_event_ids.append(conflict_event.audit_id)
            blocked_event = audit_log.record_event(
                event_type="calendar.proposal_blocked",
                workflow="calendar_dry_run",
                idempotency_key=preliminary.idempotency_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason="no_available_slot",
                sources=sources,
                artifacts={"window_start": window_start, "window_end": window_end},
            )
            audit_event_ids.append(blocked_event.audit_id)
        return {
            "status": "blocked",
            "reason": "no_available_slot",
            "audit_id": audit_event_ids[-1] if audit_event_ids else None,
            "audit_event_ids": audit_event_ids,
            "idempotency_key": preliminary.idempotency_key,
            "calendar_write_attempted": False,
            "conflict_count": len(blockers_seen),
        }

    chosen_key = stable_idempotency_key(
        kind=kind,
        day=day,
        start=slot_start.strftime("%H:%M"),
        duration_minutes=duration_minutes,
        source_refs=sources,
    )
    final_policy = evaluate_calendar_policy(
        kind=kind,
        day=day,
        start=slot_start.strftime("%H:%M"),
        duration_minutes=duration_minutes,
        source_refs=sources,
        label=label,
    )
    title = planned_title
    duplicates = [
        event for event in _blocking_events(existing_events, start=slot_start, end=slot_end)
        if _is_matching_rocky_duplicate(
            event,
            kind=kind,
            title=title,
            idempotency_key=chosen_key,
        )
    ]
    if duplicates:
        if audit_log:
            blocked_event = audit_log.record_event(
                event_type="calendar.proposal_blocked",
                workflow="calendar_dry_run",
                idempotency_key=chosen_key,
                policy_version=POLICY_VERSION,
                decision="blocked",
                reason="duplicate_rocky_block",
                sources=sources,
                artifacts={
                    "duplicate_count": len(duplicates),
                    "duplicate_signatures": [_event_signature(event) for event in duplicates],
                },
            )
            audit_event_ids.append(blocked_event.audit_id)
        return {
            "status": "blocked",
            "reason": "duplicate_rocky_block",
            "audit_id": audit_event_ids[-1] if audit_event_ids else None,
            "audit_event_ids": audit_event_ids,
            "idempotency_key": chosen_key,
            "calendar_write_attempted": False,
            "duplicate_count": len(duplicates),
            "policy_decision": final_policy.to_dict(),
        }

    proposal_event = None
    if audit_log:
        allowed_event = audit_log.record_event(
            event_type="policy.allowed",
            workflow="calendar_dry_run",
            idempotency_key=chosen_key,
            policy_version=POLICY_VERSION,
            decision="allowed",
            reason="policy_allowed",
            sources=sources,
            artifacts={"policy_decision": final_policy.to_dict()},
        )
        audit_event_ids.append(allowed_event.audit_id)
        proposal_event = audit_log.record_event(
            event_type="calendar.proposal_created",
            workflow="calendar_dry_run",
            idempotency_key=chosen_key,
            policy_version=POLICY_VERSION,
            decision="created",
            reason=reason,
            sources=sources,
            artifacts={
                "title": title,
                "start": slot_start.isoformat(),
                "end": slot_end.isoformat(),
                "calendar_write_attempted": False,
            },
        )
        audit_event_ids.append(proposal_event.audit_id)
        completed_event = audit_log.record_event(
            event_type="dry_run.completed",
            workflow="calendar_dry_run",
            idempotency_key=chosen_key,
            policy_version=POLICY_VERSION,
            decision="completed",
            reason="dry_run_only_no_calendar_write",
            sources=sources,
            artifacts={"proposal_audit_id": proposal_event.audit_id},
        )
        audit_event_ids.append(completed_event.audit_id)

    created_at = proposal_event.created_at if proposal_event else datetime.utcnow().isoformat()
    audit_id = proposal_event.audit_id if proposal_event else None
    return {
        "status": "proposal",
        "mode": "dry_run",
        "calendar_write_attempted": False,
        "audit_id": audit_id,
        "audit_event_ids": audit_event_ids,
        "idempotency_key": chosen_key,
        "title": title,
        "start": slot_start.isoformat(),
        "end": slot_end.isoformat(),
        "duration_minutes": duration_minutes,
        "confidence": confidence,
        "metadata_description": build_metadata_description(
            kind=kind,
            reason=reason,
            sources=sources,
            confidence=confidence,
            audit_id=audit_id or "dry-run-not-recorded",
            idempotency_key=chosen_key,
            created_at=created_at,
            metadata_extra=metadata_extra,
        ),
        "policy_decision": final_policy.to_dict(),
        "existing_event_count": len(existing_events),
    }
