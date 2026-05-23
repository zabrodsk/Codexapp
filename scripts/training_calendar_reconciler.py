#!/usr/bin/env python3
"""Reconcile TrainingPeaks planned workouts with Rocky-owned Calendar blocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_calendar_policy import POLICY_VERSION, combine_local, stable_idempotency_key, title_for_kind
from assistant_calendar_state import AssistantCalendarState
from assistant_calendar_status import calendar_write_health, inspect_calendar_block
from assistant_calendar_writer import create_calendar_block, delete_calendar_block
from assistant_notification_dispatcher import dispatch_failure_notification
from training_calendar_proposal_engine import (
    DEFAULT_DAYS_AHEAD,
    DEFAULT_WEBCAL_URL_FILE,
    infer_training_window,
)
from trainingpeaks_ics_reader import preview_webcal_url_file


WORKFLOW = "training_calendar_reconciler"
RECONCILE_POLICY_VERSION = "rocky-training-reconcile-v1"
UNSAFE_TEXT_RE = re.compile(
    r"(webcal://|https?://|cookie|token|secret|password|credential|auth)",
    re.IGNORECASE,
)
ATTENTION_STATUSES = {
    "manual_review_required",
    "calendar_state_stale",
    "cancelled_candidate",
    "weekend_policy_blocked",
}


def reconcile_training_calendar(
    *,
    webcal_url_file: str | Path = DEFAULT_WEBCAL_URL_FILE,
    planning_date: str | date | None = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    calendar_name: str = "Calendar",
    fix_safe: bool = False,
    live: bool = False,
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    db_path: str | Path | None = None,
    state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    preview_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    health_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now().date()
    source_payload = preview_payload
    if source_payload is None:
        source_payload = preview_webcal_url_file(
            webcal_url_file,
            start_date=planning_day,
            days_ahead=days_ahead,
        )
    if source_payload.get("status") != "ok":
        payload = _redact_payload(
            {
                "status": "blocked",
                "reason": "trainingpeaks_read_failed",
                "workflow": WORKFLOW,
                "planning_date": planning_day.isoformat(),
                "calendar_write_attempted": False,
                "manual_review_required": True,
                "results": [],
                "source_status": source_payload.get("status"),
                "source_reason": source_payload.get("reason"),
            }
        )
        _notify_if_needed(payload, enabled=notify_failures, dry_run=notification_dry_run, channel_id=notification_channel_id, ledger_path=ledger_path, scheduler_db_path=scheduler_db_path)
        return payload

    state = AssistantCalendarState(state_db_path)
    expected_blocks = [_expected_block(workout) for workout in source_payload.get("workouts", [])]
    active_records = [
        record for record in state.list_blocks(calendar_name=calendar_name, status="active")
        if str(record.get("title") or "").startswith("Rocky: Training")
    ]
    results = [
        _reconcile_record(
            record,
            expected_blocks=expected_blocks,
            state=state,
            calendar_name=calendar_name,
            fix_safe=fix_safe,
            live=live,
            db_path=db_path,
            existing_events=existing_events,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            health_payload=health_payload,
        )
        for record in active_records
    ]
    protected_keys = {result.get("expected", {}).get("idempotency_key") for result in results}
    unprotected = [
        {
            "status": "current_workout_unprotected",
            "reason": "no_active_rocky_training_block_for_current_workout",
            "expected": _safe_expected(block),
            "calendar_write_attempted": False,
        }
        for block in expected_blocks
        if block["idempotency_key"] not in protected_keys
    ]
    results.extend(unprotected)
    manual_review = any(result.get("status") in ATTENTION_STATUSES for result in results)
    write_attempted = any(bool(result.get("calendar_write_attempted")) for result in results)
    payload = _redact_payload(
        {
            "status": "manual_review_required" if manual_review else "ok",
            "reason": "attention_needed" if manual_review else "training_calendar_reconciled",
            "workflow": WORKFLOW,
            "planning_date": planning_day.isoformat(),
            "calendar_name": calendar_name,
            "fix_safe": bool(fix_safe),
            "live": bool(live),
            "source_status": source_payload.get("status"),
            "workout_count": len(expected_blocks),
            "active_training_block_count": len(active_records),
            "manual_review_required": manual_review,
            "calendar_write_attempted": write_attempted,
            "results": results,
        }
    )
    _record_reconcile_audit(payload, ledger_path=ledger_path)
    notification = _notify_if_needed(
        payload,
        enabled=notify_failures,
        dry_run=notification_dry_run,
        channel_id=notification_channel_id,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
    )
    if notification:
        payload["notification"] = notification
    return payload


def _reconcile_record(
    record: dict[str, Any],
    *,
    expected_blocks: list[dict[str, Any]],
    state: AssistantCalendarState,
    calendar_name: str,
    fix_safe: bool,
    live: bool,
    db_path: str | Path | None,
    existing_events: list[dict[str, Any]] | None,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    health_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    calendar_ok = _record_has_calendar_match(record, calendar_name=calendar_name, db_path=db_path, existing_events=existing_events)
    if not calendar_ok:
        return {
            "status": "calendar_state_stale",
            "reason": "active_state_missing_calendar_event",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "state": _safe_record(record),
            "calendar_write_attempted": False,
        }

    exact = _find_one(expected_blocks, lambda block: block["idempotency_key"] == record.get("idempotency_key"))
    if exact:
        return {
            "status": "matched_current",
            "reason": "current_trainingpeaks_workout_matches_active_calendar_block",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(exact),
            "state": _safe_record(record),
            "calendar_write_attempted": False,
        }

    aliases = state.list_aliases(canonical_idempotency_key=str(record.get("idempotency_key")))
    alias_keys = {alias["alias_idempotency_key"] for alias in aliases}
    alias_match = _find_one(expected_blocks, lambda block: block["idempotency_key"] in alias_keys)
    if alias_match:
        return {
            "status": "source_ref_drift_verified",
            "reason": "current_trainingpeaks_key_resolves_to_existing_calendar_block",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(alias_match),
            "state": _safe_record(record),
            "calendar_write_attempted": False,
        }

    same_shape = [
        block for block in expected_blocks
        if block["title"] == record.get("title") and block["start"] == record.get("start") and block["end"] == record.get("end")
    ]
    if len(same_shape) == 1:
        expected = same_shape[0]
        alias = None
        if fix_safe and live:
            alias = state.record_alias(
                alias_idempotency_key=expected["idempotency_key"],
                canonical_idempotency_key=str(record["idempotency_key"]),
                reason="source_ref_drift_verified",
                metadata={
                    "source_refs": expected["source_refs"],
                    "title": expected["title"],
                    "date": expected["date"],
                },
            )
            _record_fix_audit(
                ledger_path=ledger_path,
                idempotency_key=expected["idempotency_key"],
                reason="source_ref_drift_alias_recorded",
                artifacts={"alias": alias, "canonical_idempotency_key": record["idempotency_key"]},
            )
        return {
            "status": "source_ref_drift_verified",
            "reason": "same_training_block_with_changed_trainingpeaks_source_ref",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(expected),
            "state": _safe_record(record),
            "alias": _safe_alias(alias),
            "calendar_write_attempted": False,
        }
    if len(same_shape) > 1:
        return _manual_review(record, reason="ambiguous_source_ref_drift_match", candidates=same_shape)

    source_refs = _record_source_refs(record)
    same_source = [
        block for block in expected_blocks
        if set(block["source_refs"]) & set(source_refs)
    ]
    if len(same_source) == 1:
        return _handle_same_source_change(
            record,
            expected=same_source[0],
            fix_safe=fix_safe,
            live=live,
            calendar_name=calendar_name,
            db_path=db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            health_payload=health_payload,
        )
    if len(same_source) > 1:
        return _manual_review(record, reason="ambiguous_trainingpeaks_source_match", candidates=same_source)

    same_title = [block for block in expected_blocks if block["title"] == record.get("title")]
    if same_title:
        return _manual_review(record, reason="title_only_training_change_requires_review", candidates=same_title)

    return {
        "status": "cancelled_candidate",
        "reason": "active_training_block_has_no_current_trainingpeaks_match",
        "canonical_idempotency_key": record.get("idempotency_key"),
        "state": _safe_record(record),
        "calendar_write_attempted": False,
    }


def _handle_same_source_change(
    record: dict[str, Any],
    *,
    expected: dict[str, Any],
    fix_safe: bool,
    live: bool,
    calendar_name: str,
    db_path: str | Path | None,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    health_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if _is_weekend(expected["date"]):
        return {
            "status": "weekend_policy_blocked",
            "reason": "training_move_target_is_friday_saturday_or_sunday",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(expected),
            "state": _safe_record(record),
            "calendar_write_attempted": False,
        }
    if _intervals_overlap(record["start"], record["end"], expected["start"], expected["end"]):
        return _manual_review(record, reason="overlapping_training_move_requires_review", candidates=[expected])
    if not (fix_safe and live):
        return {
            "status": "moved_safe_fix_candidate",
            "reason": "safe_non_overlapping_training_move_detected",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(expected),
            "state": _safe_record(record),
            "calendar_write_attempted": False,
        }
    health = health_payload or calendar_write_health(db_path=db_path, ledger_path=ledger_path, write_audit=False)
    if health.get("status") != "ok":
        return {
            "status": "manual_review_required",
            "reason": "calendar_write_health_not_ok",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(expected),
            "state": _safe_record(record),
            "health": {"status": health.get("status"), "blocked_checks": list(health.get("blocked_checks") or [])},
            "calendar_write_attempted": False,
        }
    created = create_calendar_block(
        kind="training",
        day=expected["date"],
        window_start=expected["window_start"],
        window_end=expected["window_end"],
        duration_minutes=expected["duration_minutes"],
        label=expected["label"],
        reason="TrainingPeaks planned workout moved; Rocky safe reconciliation",
        confidence=expected["confidence"],
        source_refs=expected["source_refs"],
        calendar_name=calendar_name,
        live=True,
        db_path=db_path,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
    )
    if created.get("status") not in {"created", "skipped_duplicate"}:
        return {
            "status": "manual_review_required",
            "reason": "safe_move_create_failed",
            "canonical_idempotency_key": record.get("idempotency_key"),
            "expected": _safe_expected(expected),
            "state": _safe_record(record),
            "create_result": _safe_calendar_result(created),
            "calendar_write_attempted": bool(created.get("calendar_write_attempted")),
        }
    deleted = delete_calendar_block(
        idempotency_key=str(record["idempotency_key"]),
        calendar_name=calendar_name,
        live=True,
        db_path=db_path,
        scheduler_db_path=scheduler_db_path,
        ledger_path=ledger_path,
    )
    status = "moved_safe_fix_applied" if deleted.get("status") == "deleted" else "manual_review_required"
    reason = "safe_move_create_then_delete_completed" if status == "moved_safe_fix_applied" else "safe_move_delete_failed_after_create"
    _record_fix_audit(
        ledger_path=ledger_path,
        idempotency_key=expected["idempotency_key"],
        reason=reason,
        artifacts={"create_result": _safe_calendar_result(created), "delete_result": _safe_calendar_result(deleted)},
    )
    return {
        "status": status,
        "reason": reason,
        "canonical_idempotency_key": record.get("idempotency_key"),
        "expected": _safe_expected(expected),
        "state": _safe_record(record),
        "create_result": _safe_calendar_result(created),
        "delete_result": _safe_calendar_result(deleted),
        "calendar_write_attempted": True,
    }


def _expected_block(workout: dict[str, Any]) -> dict[str, Any]:
    inference = infer_training_window(workout)
    source_refs = [str(workout.get("source_ref") or "trainingpeaks:unknown")]
    start_dt = combine_local(str(workout.get("date")), inference["window_start"])
    duration = int(inference["proposal_duration_minutes"])
    end_dt = start_dt.replace() + (combine_local(str(workout.get("date")), inference["window_end"]) - combine_local(str(workout.get("date")), inference["window_start"]))
    if round((end_dt - start_dt).total_seconds() / 60) != duration:
        end_dt = start_dt.replace() + timedelta(minutes=duration)
    label = str(workout.get("title") or "planned workout")
    return {
        "idempotency_key": stable_idempotency_key(
            kind="training",
            day=str(workout.get("date")),
            start=inference["window_start"],
            duration_minutes=duration,
            source_refs=source_refs,
        ),
        "date": str(workout.get("date")),
        "title": title_for_kind("training", label=label),
        "label": label,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "window_start": inference["window_start"],
        "window_end": inference["window_end"],
        "duration_minutes": duration,
        "source_refs": source_refs,
        "confidence": str(inference.get("confidence") or "medium"),
        "warnings": list(inference.get("warnings") or []),
    }


def _record_has_calendar_match(
    record: dict[str, Any],
    *,
    calendar_name: str,
    db_path: str | Path | None,
    existing_events: list[dict[str, Any]] | None,
) -> bool:
    if existing_events is not None:
        for event in existing_events:
            if (
                str(event.get("calendar") or "") == calendar_name
                and str(event.get("summary") or "") == str(record.get("title") or "")
                and str(record.get("idempotency_key") or "") in str(event.get("description") or "")
                and "Booked by: Rocky" in str(event.get("description") or "")
            ):
                return True
        return False
    status = inspect_calendar_block(
        idempotency_key=str(record["idempotency_key"]),
        calendar_name=calendar_name,
        db_path=db_path,
    )
    return status.get("status") == "active_verified"


def _record_source_refs(record: dict[str, Any]) -> list[str]:
    try:
        metadata = json.loads(str(record.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return [str(item) for item in metadata.get("source_refs") or []]


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "idempotency_key": record.get("idempotency_key"),
            "calendar_name": record.get("calendar_name"),
            "title": record.get("title"),
            "start": record.get("start"),
            "end": record.get("end"),
            "status": record.get("status"),
            "event_uid": record.get("event_uid"),
            "source_refs": _record_source_refs(record),
        }
    )


def _safe_expected(block: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "idempotency_key": block.get("idempotency_key"),
            "date": block.get("date"),
            "title": block.get("title"),
            "start": block.get("start"),
            "end": block.get("end"),
            "window_start": block.get("window_start"),
            "window_end": block.get("window_end"),
            "duration_minutes": block.get("duration_minutes"),
            "source_refs": block.get("source_refs"),
            "confidence": block.get("confidence"),
            "warnings": block.get("warnings"),
        }
    )


def _safe_alias(alias: dict[str, Any] | None) -> dict[str, Any] | None:
    if not alias:
        return None
    return _redact_payload(
        {
            "alias_idempotency_key": alias.get("alias_idempotency_key"),
            "canonical_idempotency_key": alias.get("canonical_idempotency_key"),
            "reason": alias.get("reason"),
        }
    )


def _safe_calendar_result(result: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": result.get("status"),
            "reason": result.get("reason"),
            "idempotency_key": result.get("idempotency_key"),
            "audit_id": result.get("audit_id"),
            "calendar_write_attempted": result.get("calendar_write_attempted"),
            "calendar_event_created": result.get("calendar_event_created"),
            "calendar_event_deleted": result.get("calendar_event_deleted"),
        }
    )


def _manual_review(record: dict[str, Any], *, reason: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "manual_review_required",
        "reason": reason,
        "canonical_idempotency_key": record.get("idempotency_key"),
        "state": _safe_record(record),
        "candidate_count": len(candidates),
        "candidates": [_safe_expected(candidate) for candidate in candidates[:3]],
        "calendar_write_attempted": False,
    }


def _find_one(items: list[dict[str, Any]], predicate) -> dict[str, Any] | None:
    matches = [item for item in items if predicate(item)]
    return matches[0] if len(matches) == 1 else None


def _intervals_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    a0, a1 = datetime.fromisoformat(start_a), datetime.fromisoformat(end_a)
    b0, b1 = datetime.fromisoformat(start_b), datetime.fromisoformat(end_b)
    return a0 < b1 and b0 < a1


def _is_weekend(day: str) -> bool:
    return date.fromisoformat(day).weekday() >= 4


def _record_reconcile_audit(payload: dict[str, Any], *, ledger_path: str | Path | None) -> None:
    AssistantAuditLog(ledger_path).record_event(
        event_type="training_calendar.reconciled",
        workflow=WORKFLOW,
        idempotency_key=f"training-calendar-reconcile:{payload.get('planning_date')}",
        policy_version=RECONCILE_POLICY_VERSION,
        decision="blocked" if payload.get("manual_review_required") else "completed",
        reason=str(payload.get("reason") or "training_calendar_reconciled"),
        sources=["trainingpeaks:webcal-secret-file", "apple-calendar:Calendar"],
        artifacts={
            "status": payload.get("status"),
            "workout_count": payload.get("workout_count"),
            "active_training_block_count": payload.get("active_training_block_count"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
            "result_statuses": [result.get("status") for result in payload.get("results", [])],
        },
    )


def _record_fix_audit(*, ledger_path: str | Path | None, idempotency_key: str, reason: str, artifacts: dict[str, Any]) -> None:
    AssistantAuditLog(ledger_path).record_event(
        event_type="training_calendar.fix_applied",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=RECONCILE_POLICY_VERSION,
        decision="completed",
        reason=reason,
        sources=["trainingpeaks:webcal-secret-file", "apple-calendar:Calendar"],
        artifacts=_redact_payload(artifacts),
    )


def _notify_if_needed(
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


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], "chars": len(value)}
    return value


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile TrainingPeaks workouts with Rocky Calendar state.")
    parser.add_argument("--webcal-url-file", default=str(DEFAULT_WEBCAL_URL_FILE), dest="webcal_url_file")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD, dest="days_ahead")
    parser.add_argument("--calendar", default="Calendar", dest="calendar_name")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--fix-safe", action="store_true", dest="fix_safe")
    parser.add_argument("--live", action="store_true", dest="live")
    parser.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = reconcile_training_calendar(
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        days_ahead=args.days_ahead,
        calendar_name=args.calendar_name,
        fix_safe=args.fix_safe,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar reconcile: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
    return 1 if payload.get("status") in {"blocked", "failed", "manual_review_required"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
