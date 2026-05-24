#!/usr/bin/env python3
"""State-only safe recovery helpers for Rocky assistant production operations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog, redact_payload
from assistant_calendar_policy import POLICY_VERSION
from assistant_calendar_state import AssistantCalendarState
from assistant_calendar_status import inspect_calendar_block
from assistant_scheduler_state import AssistantSchedulerState
from weekly_calendar_hygiene import inspect_weekly_calendar_hygiene

WORKFLOW = "assistant_safe_recovery"
RECOVERY_POLICY_VERSION = "rocky-safe-recovery-v1"
VALID_ACTIONS = {"mark-calendar-stale", "update-dead-letter"}
VALID_DEAD_LETTER_STATUSES = {"recovered", "acknowledged", "ignored"}


def build_safe_recovery_candidates(
    *,
    scheduler_db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    calendar_db_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    calendar_hygiene_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hygiene = calendar_hygiene_payload or inspect_weekly_calendar_hygiene(
        state_db_path=calendar_state_db_path,
        db_path=calendar_db_path,
        ledger_path=audit_log_path,
    )
    state = AssistantSchedulerState(scheduler_db_path)
    open_dead = state.list_dead_letters(status="open", limit=100)
    candidates: list[dict[str, Any]] = []

    for item in hygiene.get("stale_state_candidates") or []:
        key = item.get("idempotency_key")
        candidates.append({
            "kind": "calendar_stale_state",
            "idempotency_key": key,
            "title": item.get("title"),
            "status": "state_only_recovery_available",
            "suggested_command": f"assistant-safe-recovery --action mark-calendar-stale --idempotency-key {key} --live --json",
        })
    for item in hygiene.get("orphan_rocky_events") or []:
        candidates.append({
            "kind": "calendar_orphan_event",
            "idempotency_key": item.get("idempotency_key"),
            "title": item.get("summary"),
            "status": "manual_review_only",
            "suggested_command": "Inspect Apple Calendar manually; Sprint 12 does not force-delete orphan events.",
        })
    for item in open_dead:
        candidates.append({
            "kind": "dead_letter",
            "dead_letter_id": item.get("dead_letter_id"),
            "job_name": item.get("job_name"),
            "failure_class": item.get("failure_class"),
            "status": "state_only_recovery_available",
            "suggested_command": f"assistant-safe-recovery --action update-dead-letter --dead-letter-id {item.get('dead_letter_id')} --status recovered --live --json",
        })

    status = "manual_review_required" if any(item.get("status") == "manual_review_only" for item in candidates) else "ok" if not candidates else "recovery_available"
    return redact_payload({
        "status": status,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "calendar_hygiene_status": hygiene.get("status"),
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
        "state_mutated": False,
    })


def run_safe_recovery_action(
    *,
    action: str | None = None,
    idempotency_key: str | None = None,
    dead_letter_id: str | None = None,
    status: str | None = None,
    live: bool = False,
    scheduler_db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    audit_log_path: str | Path | None = None,
    calendar_db_path: str | Path | None = None,
    calendar_name: str = "Calendar",
) -> dict[str, Any]:
    if not action:
        return build_safe_recovery_candidates(
            scheduler_db_path=scheduler_db_path,
            calendar_state_db_path=calendar_state_db_path,
            calendar_db_path=calendar_db_path,
            audit_log_path=audit_log_path,
        )
    if action not in VALID_ACTIONS:
        return _blocked("unsupported_recovery_action", action=action)
    if not live:
        return _blocked("live_flag_required", action=action)
    if action == "mark-calendar-stale":
        return _mark_calendar_stale(
            idempotency_key=idempotency_key,
            calendar_name=calendar_name,
            calendar_state_db_path=calendar_state_db_path,
            calendar_db_path=calendar_db_path,
            audit_log_path=audit_log_path,
        )
    return _update_dead_letter_status(
        dead_letter_id=dead_letter_id,
        status=status,
        scheduler_db_path=scheduler_db_path,
        audit_log_path=audit_log_path,
    )


def _mark_calendar_stale(
    *,
    idempotency_key: str | None,
    calendar_name: str,
    calendar_state_db_path: str | Path | None,
    calendar_db_path: str | Path | None,
    audit_log_path: str | Path | None,
) -> dict[str, Any]:
    if not idempotency_key:
        return _blocked("idempotency_key_required", action="mark-calendar-stale")
    status_payload = inspect_calendar_block(
        idempotency_key=idempotency_key,
        calendar_name=calendar_name,
        state_db_path=calendar_state_db_path,
        db_path=calendar_db_path,
    )
    if status_payload.get("status") != "stale_state_candidate":
        return _blocked("calendar_block_not_stale_state_candidate", action="mark-calendar-stale", status_payload=_safe_status(status_payload))
    state = AssistantCalendarState(calendar_state_db_path)
    row = state.mark_stale(idempotency_key=idempotency_key)
    if not row or row.get("status") != "stale":
        return _blocked("calendar_state_mark_stale_failed", action="mark-calendar-stale", idempotency_key=idempotency_key)
    event = AssistantAuditLog(audit_log_path).record_event(
        event_type="calendar.state_marked_stale",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision="recovered",
        reason="assistant_safe_recovery_mark_calendar_stale",
        artifacts={"state": _safe_state_row(row), "precheck": _safe_status(status_payload)},
    )
    return redact_payload({
        "status": "recovered",
        "reason": "calendar_state_marked_stale",
        "action": "mark-calendar-stale",
        "idempotency_key": idempotency_key,
        "state": _safe_state_row(row),
        "audit_id": event.audit_id,
        "state_mutated": True,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    })


def _update_dead_letter_status(
    *,
    dead_letter_id: str | None,
    status: str | None,
    scheduler_db_path: str | Path | None,
    audit_log_path: str | Path | None,
) -> dict[str, Any]:
    if not dead_letter_id:
        return _blocked("dead_letter_id_required", action="update-dead-letter")
    if status not in VALID_DEAD_LETTER_STATUSES:
        return _blocked("unsupported_dead_letter_status", action="update-dead-letter", allowed=sorted(VALID_DEAD_LETTER_STATUSES))
    state = AssistantSchedulerState(scheduler_db_path)
    before = state.get_dead_letter(dead_letter_id)
    if not before:
        return _blocked("dead_letter_not_found", action="update-dead-letter", dead_letter_id=dead_letter_id)
    after = state.update_dead_letter_status(dead_letter_id, status)
    event = AssistantAuditLog(audit_log_path).record_event(
        event_type="scheduler.dead_letter_resolved",
        workflow=WORKFLOW,
        idempotency_key=str((after or before).get("idempotency_key") or dead_letter_id),
        policy_version=RECOVERY_POLICY_VERSION,
        decision="recovered",
        reason=f"assistant_safe_recovery_dead_letter_{status}",
        sources=[dead_letter_id],
        artifacts={"before": _safe_dead_letter(before), "after": _safe_dead_letter(after or {})},
    )
    return redact_payload({
        "status": "recovered",
        "reason": f"dead_letter_marked_{status}",
        "action": "update-dead-letter",
        "dead_letter_id": dead_letter_id,
        "state": _safe_dead_letter(after or {}),
        "audit_id": event.audit_id,
        "state_mutated": True,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    })


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return redact_payload({
        "status": "blocked",
        "reason": reason,
        **extra,
        "state_mutated": False,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    })


def _safe_state_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ["idempotency_key", "calendar_name", "title", "start", "end", "status", "updated_at"]}


def _safe_dead_letter(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ["dead_letter_id", "job_name", "failure_class", "safe_summary", "recovery_hint", "attempts", "status", "last_failed_at", "error_hash"]}


def _safe_status(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ["status", "idempotency_key", "calendar_match_count", "recommended_action"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="State-only safe recovery helpers for Rocky.")
    parser.add_argument("--action", choices=sorted(VALID_ACTIONS))
    parser.add_argument("--idempotency-key", dest="idempotency_key")
    parser.add_argument("--dead-letter-id", dest="dead_letter_id")
    parser.add_argument("--status", choices=sorted(VALID_DEAD_LETTER_STATUSES))
    parser.add_argument("--calendar", default="Calendar", dest="calendar_name")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--calendar-db-path", dest="calendar_db_path")
    parser.add_argument("--audit-ledger", dest="audit_ledger")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_safe_recovery_action(
        action=args.action,
        idempotency_key=args.idempotency_key,
        dead_letter_id=args.dead_letter_id,
        status=args.status,
        live=args.live,
        scheduler_db_path=args.state_db,
        calendar_state_db_path=args.calendar_state_db,
        audit_log_path=args.audit_ledger,
        calendar_db_path=args.calendar_db_path,
        calendar_name=args.calendar_name,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else payload.get("summary", payload.get("reason", payload.get("status"))))
    return 0 if payload.get("status") in {"ok", "recovery_available", "manual_review_required", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
