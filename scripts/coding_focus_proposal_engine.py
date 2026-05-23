#!/usr/bin/env python3
"""Dry-run coding focus calendar proposals for Rocky."""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_calendar_dry_run import build_calendar_dry_run
from coding_work_briefing_builder import build_coding_work_briefing


TIMEZONE = "Europe/Prague"
PREFERRED_WINDOW_START = "12:30"
PREFERRED_WINDOW_END = "19:30"
DEFAULT_DURATION_MINUTES = 90
MAX_BLOCKS = 2
MAX_TOTAL_MINUTES = 180
BOOKING_REASON = "Focused coding work selected from Rocky's sanitized coding briefing"


def build_coding_focus_proposals(
    *,
    planning_date: str | date | None = None,
    briefing_payload: dict[str, Any] | None = None,
    work_items: list[dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
    existing_events: list[dict[str, Any]] | None = None,
    max_blocks: int = MAX_BLOCKS,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    base = {
        "mode": "dry_run",
        "planning_date": planning_day.isoformat(),
        "target_date": planning_day.isoformat(),
        "timezone": TIMEZONE,
        "calendar_write_attempted": False,
        "proposals": [],
    }
    if planning_day.weekday() >= 4:
        return {**base, "status": "skipped_weekend_target", "reason": "proactive_booking_blocked_on_friday_saturday_sunday"}
    if work_items is None:
        briefing_payload = briefing_payload or build_coding_work_briefing(planning_date=planning_day)
        work_items = briefing_payload.get("selected_focus_items") or []
    eligible = [_item for _item in work_items if _eligible(_item)]
    if not eligible:
        return {**base, "status": "skipped_no_coding_focus", "reason": "no_eligible_coding_focus_candidates"}
    if _has_ambiguous_tie(eligible):
        return {**base, "status": "blocked", "reason": "ambiguous_tied_coding_focus_candidates", "blocked_count": len(eligible)}
    day_events = existing_events
    if day_events is None:
        day_events = query_events(
            db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
            start=datetime.combine(planning_day, time(0, 0)),
            end=datetime.combine(planning_day, time(23, 59, 59)),
            include_all_day=False,
        )
    proposals = []
    total_minutes = 0
    working_events = list(day_events)
    for item in eligible[: max(1, int(max_blocks))]:
        duration = _duration_for_item(item, remaining=max(0, MAX_TOTAL_MINUTES - total_minutes))
        if duration < 60:
            break
        proposal = _proposal_for_item(
            item,
            planning_day=planning_day,
            duration_minutes=duration,
            existing_events=working_events,
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
        )
        proposals.append(proposal)
        if proposal.get("status") == "proposal":
            total_minutes += duration
            working_events.append(_event_from_proposal(proposal))
    created = [item for item in proposals if item.get("status") == "proposal"]
    if created:
        return {
            **base,
            "status": "proposal",
            "reason": None,
            "selected_count": len(created),
            "total_minutes": total_minutes,
            "proposals": proposals,
            "idempotency_keys": [item.get("idempotency_key") for item in created],
            "audit_ids": [item.get("audit_id") for item in created],
        }
    return {**base, "status": "blocked", "reason": proposals[-1].get("reason") if proposals else "coding_focus_proposal_blocked", "proposals": proposals, "blocked_count": len(proposals)}


def build_coding_focus_description(item: dict[str, Any], *, audit_id: str | None, idempotency_key: str | None) -> dict[str, Any]:
    return {
        "focus_for_this_block": _safe(item.get("title") or item.get("project") or "Coding focus"),
        "where_you_left_off": _safe(item.get("where_left_off") or "Review the referenced session and repo state."),
        "recommended_next_step": _safe(item.get("recommended_next_step") or "Continue the highest-confidence unfinished coding thread."),
        "done_signal": _safe(item.get("done_signal") or "Commit, handoff, or mark the work item done."),
        "relevant_references": ", ".join(str(ref) for ref in (item.get("source_refs") or item.get("evidence_refs") or [])[:5]) or "none",
        "work_item_id": item.get("work_item_id") or "",
        "project": item.get("project") or "",
        "audit_id": audit_id or "dry-run-not-recorded",
        "idempotency_key": idempotency_key or "pending",
    }


def _proposal_for_item(
    item: dict[str, Any],
    *,
    planning_day: date,
    duration_minutes: int,
    existing_events: list[dict[str, Any]],
    db_path: str | Path | None,
    ledger_path: str | Path | None,
    write_audit: bool,
) -> dict[str, Any]:
    source_refs = item.get("source_refs") or item.get("evidence_refs") or [item.get("work_item_id")]
    metadata = build_coding_focus_description(item, audit_id=None, idempotency_key=None)
    proposal = build_calendar_dry_run(
        kind="coding_focus",
        day=planning_day.isoformat(),
        window_start=PREFERRED_WINDOW_START,
        window_end=PREFERRED_WINDOW_END,
        duration_minutes=duration_minutes,
        label=str(item.get("project") or "coding"),
        reason=BOOKING_REASON,
        source_refs=source_refs,
        confidence="high" if float(item.get("confidence") or 0) >= 0.85 else "medium",
        metadata_extra=metadata,
        db_path=db_path,
        existing_events=existing_events,
        ledger_path=ledger_path,
        record_audit=write_audit,
    )
    metadata = build_coding_focus_description(item, audit_id=proposal.get("audit_id"), idempotency_key=proposal.get("idempotency_key"))
    description = _description_text(metadata, proposal.get("metadata_description"))
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "idempotency_key": proposal.get("idempotency_key"),
        "audit_id": proposal.get("audit_id"),
        "title": proposal.get("title"),
        "start": proposal.get("start"),
        "end": proposal.get("end"),
        "duration_minutes": duration_minutes,
        "metadata_extra": metadata,
        "metadata_description": description,
        "selected_work_item": _safe_item(item),
        "calendar_write_attempted": False,
        "proposal": {key: proposal.get(key) for key in ("status", "reason", "title", "start", "end", "confidence")},
    }


def _description_text(metadata: dict[str, Any], base_description: str | None) -> str:
    lines = [
        "Focus for this block:",
        str(metadata.get("focus_for_this_block") or "Coding focus"),
        "",
        "Where you left off:",
        str(metadata.get("where_you_left_off") or "Review recent sanitized coding signals."),
        "",
        "Recommended next step:",
        str(metadata.get("recommended_next_step") or "Continue the unfinished coding thread."),
        "",
        "Done signal:",
        str(metadata.get("done_signal") or "Commit, handoff, or mark done."),
        "",
        "Relevant references:",
        str(metadata.get("relevant_references") or "none"),
    ]
    if base_description:
        lines.extend(["", base_description])
    return "\n".join(lines)


def _event_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": proposal.get("title") or "Rocky: Coding focus",
        "description": proposal.get("metadata_description") or "",
        "start_local": str(proposal.get("start") or "").replace("T", " ")[:19],
        "end_local": str(proposal.get("end") or "").replace("T", " ")[:19],
        "all_day": False,
        "calendar": "Calendar",
    }


def _eligible(item: dict[str, Any]) -> bool:
    return (
        float(item.get("confidence") or 0) >= 0.75
        and str(item.get("status") or "active") == "active"
        and not bool(item.get("requires_dusan_decision"))
        and not bool(item.get("prompt_injection_flagged"))
    )


def _has_ambiguous_tie(items: list[dict[str, Any]]) -> bool:
    if len(items) < 3:
        return False
    top = sorted(items, key=lambda item: (str(item.get("priority") or "Normal"), -float(item.get("confidence") or 0)))[:3]
    return len({(item.get("priority"), item.get("confidence")) for item in top}) == 1


def _duration_for_item(item: dict[str, Any], *, remaining: int) -> int:
    requested = int(item.get("estimated_effort_minutes") or DEFAULT_DURATION_MINUTES)
    duration = max(60, min(120, requested, remaining or requested))
    return ((duration + 14) // 15) * 15


def _safe_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("work_item_id", "project", "title", "priority", "confidence", "requires_dusan_decision", "where_left_off", "recommended_next_step", "source_refs")}


def _safe(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    blocked = ["token", "secret", "password", "credential", "cookie", "Bearer ", "sk-"]
    if any(part.lower() in text.lower() for part in blocked):
        return "[redacted]"
    return text[:limit]


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()
