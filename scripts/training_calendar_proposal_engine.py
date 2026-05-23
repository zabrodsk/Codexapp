#!/usr/bin/env python3
"""Dry-run TrainingPeaks to Rocky training-calendar proposals.

Sprint 4.1 intentionally stops at proposals. It reads planned workouts from the
read-only TrainingPeaks ICS path, infers a conservative morning protection
window, and delegates policy/conflict/duplicate checks to the existing calendar
dry-run layer.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from assistant_calendar_dry_run import build_calendar_dry_run
from trainingpeaks_ics_reader import TIMEZONE, preview_webcal_url_file


DEFAULT_WEBCAL_URL_FILE = Path("/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url")
DEFAULT_TARGET_WORKING_DAYS = 3
DEFAULT_DAYS_AHEAD = 14
DATE_ONLY_WINDOW_START = "08:00"
DATE_ONLY_WINDOW_END = "10:30"
FULL_MORNING_DURATION_MINUTES = 150
BLOCK_SCOPE = "full_morning"
SECRET_FIELD_RE = re.compile(r"(cookie|token|secret|password|credential|auth)", re.IGNORECASE)
SPORT_DURATION_FALLBACKS = {
    "run": 75,
    "bike": 90,
    "swim": 75,
    "strength": 60,
    "mobility": 45,
    "walk": 45,
}


def add_working_days(start_day: str | date, working_days: int) -> date:
    current = _parse_date(start_day)
    remaining = int(working_days)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def parse_title_duration_minutes(title: str) -> int | None:
    safe_title = str(title or "")

    interval_match = re.search(
        r"\b(\d{1,2})\s*x\s*(\d{1,3})\s*(?:min|mins|minute|minutes)\b",
        safe_title,
        re.IGNORECASE,
    )
    if interval_match:
        return int(interval_match.group(1)) * int(interval_match.group(2))

    clock_match = re.search(r"\b(\d{1,2}):(\d{2})\b", safe_title)
    if clock_match:
        return int(clock_match.group(1)) * 60 + int(clock_match.group(2))

    hours_match = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|hour|hours)\b",
        safe_title,
        re.IGNORECASE,
    )
    if hours_match:
        return round(float(hours_match.group(1).replace(",", ".")) * 60)

    minutes_match = re.search(
        r"\b(\d{1,3})\s*(?:min|mins|minute|minutes)\b",
        safe_title,
        re.IGNORECASE,
    )
    if minutes_match:
        return int(minutes_match.group(1))

    return None


def build_training_calendar_proposals(
    *,
    webcal_url_file: str | Path = DEFAULT_WEBCAL_URL_FILE,
    planning_date: str | date | None = None,
    target_working_days: int = DEFAULT_TARGET_WORKING_DAYS,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
    preview_payload: dict[str, Any] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    target_date: str | date | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(tz=ZoneInfo(TIMEZONE)).date()
    target_day = _parse_date(target_date) if target_date else add_working_days(planning_day, target_working_days)

    source_payload = preview_payload
    if source_payload is None:
        source_payload = preview_webcal_url_file(
            webcal_url_file,
            days_ahead=days_ahead,
            start_date=planning_day,
        )

    if source_payload.get("status") != "ok":
        return {
            "status": "blocked",
            "reason": "trainingpeaks_read_failed",
            "mode": "dry_run",
            "calendar_write_attempted": False,
            "planning_date": planning_day.isoformat(),
            "target_date": target_day.isoformat(),
            "target_working_days": int(target_working_days),
            "source_status": source_payload.get("status"),
            "source_reason": source_payload.get("reason"),
            "warnings": source_payload.get("warnings") or [],
            "proposals": [],
        }

    selected_workouts = [
        workout
        for workout in source_payload.get("workouts", [])
        if str(workout.get("date")) == target_day.isoformat()
    ]
    proposals = [
        _build_one_training_proposal(
            workout,
            db_path=db_path,
            ledger_path=ledger_path,
            write_audit=write_audit,
            existing_events=existing_events,
        )
        for workout in selected_workouts
    ]
    statuses = {proposal.get("status") for proposal in proposals}
    if not proposals:
        status = "no_workout"
    elif statuses == {"proposal"}:
        status = "proposal"
    elif "proposal" in statuses:
        status = "partial"
    else:
        status = "blocked"
    reason = _payload_reason(status, proposals)

    return {
        "status": status,
        "reason": reason,
        "mode": "dry_run",
        "calendar_write_attempted": False,
        "planning_date": planning_day.isoformat(),
        "target_date": target_day.isoformat(),
        "target_working_days": int(target_working_days),
        "days_ahead": int(days_ahead),
        "timezone": TIMEZONE,
        "source": source_payload.get("source"),
        "source_status": source_payload.get("status"),
        "selected_workout_count": len(selected_workouts),
        "warnings": _unique(
            list(source_payload.get("warnings") or [])
            + [warning for proposal in proposals for warning in proposal.get("warnings", [])]
        ),
        "proposals": proposals,
    }


def _build_one_training_proposal(
    workout: dict[str, Any],
    *,
    db_path: str | Path | None,
    ledger_path: str | Path | None,
    write_audit: bool,
    existing_events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    inference = infer_training_window(workout)
    safe_workout = _safe_workout_summary(workout)
    proposal = build_calendar_dry_run(
        kind="training",
        day=safe_workout["date"],
        window_start=inference["window_start"],
        window_end=inference["window_end"],
        duration_minutes=inference["proposal_duration_minutes"],
        label=safe_workout["title"],
        reason="TrainingPeaks planned workout 3 working days ahead",
        source_refs=[safe_workout["source_ref"]],
        confidence=inference["confidence"],
        db_path=db_path,
        existing_events=existing_events,
        ledger_path=ledger_path,
        record_audit=write_audit,
    )
    return {
        "status": proposal.get("status"),
        "reason": proposal.get("reason"),
        "workout": safe_workout,
        "inference": inference,
        "proposal": proposal,
        "audit_id": proposal.get("audit_id"),
        "audit_event_ids": proposal.get("audit_event_ids") or [],
        "idempotency_key": proposal.get("idempotency_key"),
        "calendar_write_attempted": False,
        "warnings": _unique(list(safe_workout.get("warnings") or []) + list(inference.get("warnings") or [])),
    }


def infer_training_window(workout: dict[str, Any]) -> dict[str, Any]:
    title = _safe_title(workout.get("title"))
    parsed_duration = parse_title_duration_minutes(title)
    fallback_duration = SPORT_DURATION_FALLBACKS.get(str(workout.get("sport") or "").lower(), 75)
    warnings = list(workout.get("warnings") or [])
    planned_start = workout.get("planned_start_local")
    planned_end = workout.get("planned_end_local")

    if planned_start and planned_end:
        start_dt = _parse_iso_datetime(planned_start)
        end_dt = _parse_iso_datetime(planned_end)
        window_start = start_dt.strftime("%H:%M")
        window_end = end_dt.strftime("%H:%M")
        window_minutes = max(1, round((end_dt - start_dt).total_seconds() / 60))
        confidence = "high"
        timing_source = "trainingpeaks_timed_workout"
    else:
        window_start = DATE_ONLY_WINDOW_START
        window_end = DATE_ONLY_WINDOW_END
        window_minutes = FULL_MORNING_DURATION_MINUTES
        warnings.append("date_only_workout_time_inferred")
        if parsed_duration is not None:
            confidence = "medium"
            timing_source = "date_only_title_duration"
        else:
            confidence = "low"
            timing_source = "date_only_sport_fallback"

    proposal_duration = min(FULL_MORNING_DURATION_MINUTES, window_minutes)
    duration_source = "title" if parsed_duration is not None else "sport_fallback"
    inferred_duration = parsed_duration if parsed_duration is not None else fallback_duration
    return {
        "block_scope": BLOCK_SCOPE,
        "window_start": window_start,
        "window_end": window_end,
        "proposal_duration_minutes": proposal_duration,
        "inferred_workout_duration_minutes": inferred_duration,
        "duration_source": duration_source,
        "confidence": confidence,
        "timing_source": timing_source,
        "warnings": _unique(warnings),
    }


def _safe_workout_summary(workout: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": workout.get("source"),
        "source_ref": str(workout.get("source_ref") or "trainingpeaks:unknown"),
        "date": str(workout.get("date")),
        "title": _safe_title(workout.get("title")),
        "sport": workout.get("sport"),
        "confidence": workout.get("confidence"),
        "planned_start_local": workout.get("planned_start_local"),
        "planned_end_local": workout.get("planned_end_local"),
        "planned_duration_minutes": workout.get("planned_duration_minutes"),
        "warnings": list(workout.get("warnings") or []),
        "observed_at": workout.get("observed_at"),
    }


def _payload_reason(status: str, proposals: list[dict[str, Any]]) -> str | None:
    if status == "no_workout":
        return "no_trainingpeaks_workout_on_target_date"
    if status != "blocked":
        return None
    for proposal in proposals:
        policy_reasons = (
            ((proposal.get("proposal") or {}).get("policy_decision") or {}).get("reasons") or []
        )
        if "proactive_booking_blocked_on_friday_saturday_sunday" in policy_reasons:
            return "proactive_booking_blocked_on_friday_saturday_sunday"
    reasons = [proposal.get("reason") for proposal in proposals if proposal.get("reason")]
    if reasons and all(reason == reasons[0] for reason in reasons):
        return reasons[0]
    return "all_training_proposals_blocked"


def _safe_title(value: Any) -> str:
    title = " ".join(str(value or "Planned workout").split())
    if SECRET_FIELD_RE.search(title):
        return "Planned workout"
    return title[:120] or "Planned workout"


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return parsed.astimezone(ZoneInfo(TIMEZONE))


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


__all__ = [
    "DEFAULT_DAYS_AHEAD",
    "DEFAULT_TARGET_WORKING_DAYS",
    "DEFAULT_WEBCAL_URL_FILE",
    "add_working_days",
    "build_training_calendar_proposals",
    "infer_training_window",
    "parse_title_duration_minutes",
]
