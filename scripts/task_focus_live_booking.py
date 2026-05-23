#!/usr/bin/env python3
"""Supervised live booking for Rocky task focus proposals."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant_calendar_writer import create_calendar_block
from notion_task_manager import mark_task_calendar_status
from task_focus_proposal_engine import build_task_focus_proposals


def book_task_focus_proposal(
    *,
    idempotency_key: str,
    planning_date: str | None = None,
    calendar_name: str = "Calendar",
    live: bool = False,
    tasks: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    notion_client: Any | None = None,
    notion_config: Any | None = None,
) -> dict[str, Any]:
    if not live:
        return {
            "status": "blocked",
            "reason": "live_flag_required",
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
        }
    proposals = build_task_focus_proposals(
        planning_date=planning_date,
        tasks=tasks,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=True,
        existing_events=existing_events,
    )
    selected = None
    for item in proposals.get("proposals") or []:
        if item.get("idempotency_key") == idempotency_key:
            selected = item
            break
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
            "reason": str(selected.get("reason") or "task_focus_proposal_blocked"),
            "idempotency_key": idempotency_key,
            "selected_proposal": selected,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
        }
    calendar_proposal = selected.get("proposal") or {}
    result = create_calendar_block(
        kind="task_focus",
        day=str(proposals.get("target_date")),
        window_start=str(selected.get("window_start")),
        window_end=str(selected.get("window_end")),
        duration_minutes=int(selected.get("duration_minutes") or calendar_proposal.get("duration_minutes") or 30),
        label=str((selected.get("selected_task") or {}).get("title") or "task"),
        reason="Focused time for Rocky-tracked personal task",
        confidence=str(calendar_proposal.get("confidence") or "medium"),
        source_refs=[(selected.get("selected_task") or {}).get("source_ref") or "notion-task"],
        calendar_name=calendar_name,
        live=True,
        db_path=db_path,
        state_db_path=state_db_path,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
    )
    task = selected.get("selected_task") or {}
    if (
        result.get("status") in {"created", "skipped_duplicate"}
        and task.get("page_id")
        and (notion_client is not None or notion_config is not None)
    ):
        status = "Scheduled" if result.get("status") == "created" else "Skipped"
        result["notion_calendar_status_update"] = mark_task_calendar_status(
            page_id=str(task["page_id"]),
            calendar_status=status,
            idempotency_key=idempotency_key,
            config=notion_config,
            client=notion_client,
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
