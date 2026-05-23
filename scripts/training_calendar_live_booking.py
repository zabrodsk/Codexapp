#!/usr/bin/env python3
"""Supervised live booking for TrainingPeaks-derived Rocky training blocks."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from assistant_calendar_state import AssistantCalendarState
from assistant_calendar_status import calendar_write_health
from assistant_calendar_writer import create_calendar_block
from training_calendar_proposal_engine import (
    DEFAULT_DAYS_AHEAD,
    DEFAULT_TARGET_WORKING_DAYS,
    DEFAULT_WEBCAL_URL_FILE,
    build_training_calendar_proposals,
)


BOOKING_REASON = "TrainingPeaks planned workout 3 working days ahead"
UNSAFE_SOURCE_RE = re.compile(r"(webcal://|https?://|cookie|token|secret|password|credential|auth)", re.IGNORECASE)


def book_training_calendar_proposal(
    *,
    idempotency_key: str,
    webcal_url_file: str | Path = DEFAULT_WEBCAL_URL_FILE,
    planning_date: str | None = None,
    target_working_days: int = DEFAULT_TARGET_WORKING_DAYS,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    calendar_name: str = "Calendar",
    live: bool = False,
    db_path: str | Path | None = None,
    state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    preview_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    health_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book exactly one selected dry-run proposal, guarded by an explicit live flag."""
    if not live:
        return _blocked(
            idempotency_key=idempotency_key,
            reason="live_flag_required",
            mode="dry_run",
        )

    health = health_payload
    if health is None:
        health = calendar_write_health(
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=False,
        )
    if health.get("status") != "ok":
        return _blocked(
            idempotency_key=idempotency_key,
            reason="calendar_write_health_not_ok",
            health=_safe_health_summary(health),
        )

    proposal_payload = build_training_calendar_proposals(
        webcal_url_file=webcal_url_file,
        planning_date=planning_date,
        target_working_days=target_working_days,
        days_ahead=days_ahead,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=False,
        preview_payload=preview_payload,
        existing_events=existing_events,
    )
    if proposal_payload.get("status") == "blocked" and proposal_payload.get("reason") == "trainingpeaks_read_failed":
        return _blocked(
            idempotency_key=idempotency_key,
            reason="trainingpeaks_read_failed",
            proposal_payload=_safe_proposal_payload_summary(proposal_payload),
        )

    selected = _find_selected_proposal(proposal_payload, idempotency_key)
    if selected is None:
        return _blocked(
            idempotency_key=idempotency_key,
            reason="training_calendar_proposal_not_found",
            proposal_payload=_safe_proposal_payload_summary(proposal_payload),
            available_idempotency_keys=[
                proposal.get("idempotency_key")
                for proposal in proposal_payload.get("proposals", [])
                if proposal.get("idempotency_key")
            ],
        )

    selected_summary = _safe_selected_proposal(selected)
    if _selected_has_unsafe_source(selected):
        return _blocked(
            idempotency_key=idempotency_key,
            reason="unsafe_training_source_ref",
            selected_proposal=selected_summary,
        )
    selected_is_existing_active_duplicate = (
        selected.get("reason") == "duplicate_rocky_block"
        and _has_active_calendar_state(idempotency_key=idempotency_key, state_db_path=state_db_path)
    )
    if selected.get("status") != "proposal" and not selected_is_existing_active_duplicate:
        return _blocked(
            idempotency_key=idempotency_key,
            reason=str(selected.get("reason") or "training_calendar_proposal_not_bookable"),
            selected_proposal=selected_summary,
        )

    workout = selected.get("workout") or {}
    inference = selected.get("inference") or {}
    calendar_result = create_calendar_block(
        kind="training",
        day=str(workout.get("date")),
        window_start=str(inference.get("window_start")),
        window_end=str(inference.get("window_end")),
        duration_minutes=int(inference.get("proposal_duration_minutes")),
        label=str(workout.get("title") or "planned workout"),
        reason=BOOKING_REASON,
        confidence=str(inference.get("confidence") or "medium"),
        source_refs=[workout.get("source_ref")],
        calendar_name=calendar_name,
        live=True,
        state_db_path=state_db_path,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        db_path=db_path,
    )
    write_attempted = bool(calendar_result.get("calendar_write_attempted"))
    return {
        "status": calendar_result.get("status"),
        "reason": calendar_result.get("reason"),
        "mode": "live" if write_attempted else "preflight",
        "idempotency_key": idempotency_key,
        "calendar_name": calendar_name,
        "selected_proposal": selected_summary,
        "calendar_result": _safe_calendar_result(calendar_result),
        "audit_id": calendar_result.get("audit_id"),
        "calendar_write_attempted": write_attempted,
        "calendar_event_created": bool(calendar_result.get("calendar_event_created")),
        "calendar_event_deleted": bool(calendar_result.get("calendar_event_deleted")),
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
    return _redact_payload(payload)


def _find_selected_proposal(proposal_payload: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
    for proposal in proposal_payload.get("proposals", []):
        if proposal.get("idempotency_key") == idempotency_key:
            return proposal
    return None


def _safe_health_summary(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": health.get("status"),
        "blocked_checks": list(health.get("blocked_checks") or []),
    }


def _safe_proposal_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "mode": payload.get("mode"),
            "planning_date": payload.get("planning_date"),
            "target_date": payload.get("target_date"),
            "selected_workout_count": payload.get("selected_workout_count"),
            "calendar_write_attempted": payload.get("calendar_write_attempted"),
        }
    )


def _safe_selected_proposal(selected: dict[str, Any]) -> dict[str, Any]:
    workout = selected.get("workout") or {}
    inference = selected.get("inference") or {}
    proposal = selected.get("proposal") or {}
    return _redact_payload(
        {
            "status": selected.get("status"),
            "reason": selected.get("reason"),
            "idempotency_key": selected.get("idempotency_key"),
            "audit_id": selected.get("audit_id"),
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
                "block_scope": inference.get("block_scope"),
                "window_start": inference.get("window_start"),
                "window_end": inference.get("window_end"),
                "proposal_duration_minutes": inference.get("proposal_duration_minutes"),
                "inferred_workout_duration_minutes": inference.get("inferred_workout_duration_minutes"),
                "duration_source": inference.get("duration_source"),
                "confidence": inference.get("confidence"),
                "timing_source": inference.get("timing_source"),
                "warnings": list(inference.get("warnings") or []),
            },
            "proposal": {
                "status": proposal.get("status"),
                "reason": proposal.get("reason"),
                "title": proposal.get("title"),
                "start": proposal.get("start"),
                "end": proposal.get("end"),
                "calendar_write_attempted": proposal.get("calendar_write_attempted"),
            },
        }
    )


def _safe_calendar_result(result: dict[str, Any]) -> dict[str, Any]:
    safe = dict(result)
    if "metadata_description" in safe:
        safe["metadata_description"] = {
            "redacted": True,
            "reason": "calendar_metadata_not_repeated_in_training_booking_output",
        }
    return _redact_payload(safe)


def _selected_has_unsafe_source(selected: dict[str, Any]) -> bool:
    workout = selected.get("workout") or {}
    source_ref = str(workout.get("source_ref") or "")
    return bool(UNSAFE_SOURCE_RE.search(source_ref))


def _has_active_calendar_state(*, idempotency_key: str, state_db_path: str | Path | None) -> bool:
    try:
        return AssistantCalendarState(state_db_path).get_active(idempotency_key) is not None
    except Exception:
        return False


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_SOURCE_RE.search(value):
        return {
            "redacted": True,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
            "chars": len(value),
        }
    return value


__all__ = ["book_training_calendar_proposal"]
