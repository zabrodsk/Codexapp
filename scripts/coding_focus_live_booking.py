#!/usr/bin/env python3
"""Supervised/live booking for Rocky coding focus proposals."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_calendar_writer import create_calendar_block
from coding_focus_proposal_engine import BOOKING_REASON, build_coding_focus_proposals


def book_coding_focus_proposal(
    *,
    idempotency_key: str,
    planning_date: str | None = None,
    calendar_name: str = "Calendar",
    live: bool = False,
    work_items: list[dict[str, Any]] | None = None,
    briefing_payload: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    existing_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not live:
        return {
            "status": "blocked",
            "reason": "live_flag_required",
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
        }
    proposals = build_coding_focus_proposals(
        planning_date=planning_date,
        briefing_payload=briefing_payload,
        work_items=work_items,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=True,
        existing_events=existing_events,
    )
    selected = next((item for item in proposals.get("proposals") or [] if item.get("idempotency_key") == idempotency_key), None)
    if selected is None:
        return {
            "status": "blocked",
            "reason": "idempotency_key_not_in_current_proposals",
            "idempotency_key": idempotency_key,
            "proposal_payload": _safe_proposals(proposals),
            "calendar_write_attempted": False,
            "calendar_event_created": False,
        }
    if selected.get("status") != "proposal":
        return {
            "status": "blocked",
            "reason": str(selected.get("reason") or "coding_focus_proposal_blocked"),
            "idempotency_key": idempotency_key,
            "selected_proposal": selected,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
        }
    item = selected.get("selected_work_item") or {}
    result = create_calendar_block(
        kind="coding_focus",
        day=str(proposals.get("target_date")),
        window_start=str(selected.get("start") or "")[11:16] or "12:30",
        window_end="19:30",
        duration_minutes=int(selected.get("duration_minutes") or 90),
        label=str(item.get("project") or "coding"),
        reason=BOOKING_REASON,
        confidence="high" if float(item.get("confidence") or 0) >= 0.85 else "medium",
        source_refs=item.get("source_refs") or [item.get("work_item_id") or "coding-work"],
        metadata_extra=selected.get("metadata_extra") or {},
        calendar_name=calendar_name,
        live=True,
        db_path=db_path,
        state_db_path=state_db_path,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
    )
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "idempotency_key": idempotency_key,
        "selected_proposal": selected,
        "calendar_result": result,
        "audit_id": result.get("audit_id"),
        "calendar_write_attempted": bool(result.get("calendar_write_attempted")),
        "calendar_event_created": bool(result.get("calendar_event_created")),
        "calendar_event_deleted": bool(result.get("calendar_event_deleted")),
    }


def _safe_proposals(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "target_date": payload.get("target_date"),
        "proposal_count": len(payload.get("proposals") or []),
        "idempotency_keys": [item.get("idempotency_key") for item in payload.get("proposals") or [] if item.get("idempotency_key")],
    }
