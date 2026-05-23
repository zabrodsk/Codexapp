#!/usr/bin/env python3
"""Live Apple Calendar writer for Rocky-owned calendar blocks.

This module is intentionally thin: it reuses Sprint 1 dry-run policy/conflict
checks, writes only with an explicit live flag, and only deletes events that
carry Rocky ownership metadata and the requested idempotency key.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import time
from typing import Any

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_audit_log import AssistantAuditLog
from assistant_calendar_dry_run import build_calendar_dry_run, build_metadata_description
from assistant_calendar_policy import POLICY_VERSION
from assistant_calendar_state import AssistantCalendarState
from assistant_run_lock import acquire_run_lock, release_run_lock


WORKFLOW = "calendar_live_write"
DEFAULT_CALENDAR_NAME = "Calendar"
SCRIPT_TIMEOUT_SECONDS = 30
VERIFY_ATTEMPTS = 3
VERIFY_SLEEP_SECONDS = 1.0


CREATE_EVENT_SCRIPT = """
on makeDate(yText, mText, dText, hText, minText)
    set monthList to {January, February, March, April, May, June, July, August, September, October, November, December}
    set dateValue to current date
    set year of dateValue to (yText as integer)
    set month of dateValue to item (mText as integer) of monthList
    set day of dateValue to (dText as integer)
    set time of dateValue to ((hText as integer) * hours + (minText as integer) * minutes)
    return dateValue
end makeDate

on run argv
    set calendarName to item 1 of argv
    set eventTitle to item 2 of argv
    set eventDescription to item 3 of argv
    set startDate to makeDate(item 4 of argv, item 5 of argv, item 6 of argv, item 7 of argv, item 8 of argv)
    set endDate to makeDate(item 9 of argv, item 10 of argv, item 11 of argv, item 12 of argv, item 13 of argv)
    tell application "Calendar"
        launch
        set targetCalendar to calendar calendarName
        set newEvent to make new event at end of events of targetCalendar with properties {summary:eventTitle, start date:startDate, end date:endDate, description:eventDescription}
        return uid of newEvent
    end tell
end run
"""


DELETE_EVENT_SCRIPT = """
on run argv
    set calendarName to item 1 of argv
    set targetTitle to item 2 of argv
    set targetUid to item 3 of argv
    set targetKey to item 4 of argv
    tell application "Calendar"
        launch
        set targetCalendar to calendar calendarName
        set deletedCount to 0
        if targetUid is "" then
            set candidateEvents to events of targetCalendar
        else
            set candidateEvents to every event of targetCalendar whose uid is targetUid
        end if
        repeat with currentEvent in candidateEvents
            set shouldDelete to false
            try
                set eventSummary to summary of currentEvent as text
                set eventDescription to description of currentEvent as text
                set eventUid to uid of currentEvent as text
                if eventSummary is targetTitle and eventDescription contains "Booked by: Rocky" and eventDescription contains targetKey then
                    if targetUid is "" or eventUid is targetUid then
                        set shouldDelete to true
                    end if
                end if
            end try
            if shouldDelete then
                delete currentEvent
                set deletedCount to deletedCount + 1
            end if
        end repeat
        return deletedCount as text
    end tell
end run
"""


DELETE_EVENTKIT_SCRIPT = r"""
import EventKit
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
if args.count < 6 {
    fail("missing arguments")
}

let calendarName = args[1]
let title = args[2]
let idempotencyKey = args[3]
let startISO = args[4]
let endISO = args[5]
let formatter = ISO8601DateFormatter()
guard let start = formatter.date(from: startISO), let end = formatter.date(from: endISO) else {
    fail("invalid ISO date")
}

let status = EKEventStore.authorizationStatus(for: .event)
if status.rawValue == 0 {
    fail("calendar access not determined")
}
if status.rawValue == 1 || status.rawValue == 2 {
    fail("calendar access denied")
}

let store = EKEventStore()
let calendars = store.calendars(for: .event).filter { $0.title == calendarName }
if calendars.isEmpty {
    fail("calendar not found")
}

let predicate = store.predicateForEvents(
    withStart: start.addingTimeInterval(-60),
    end: end.addingTimeInterval(60),
    calendars: calendars
)
let events = store.events(matching: predicate)
var deletedCount = 0
for event in events {
    let notes = event.notes ?? ""
    if event.title == title && notes.contains("Booked by: Rocky") && notes.contains(idempotencyKey) {
        do {
            try store.remove(event, span: .thisEvent, commit: false)
            deletedCount += 1
        } catch {
            fail("remove failed: \(error.localizedDescription)")
        }
    }
}
if deletedCount > 0 {
    do {
        try store.commit()
    } catch {
        fail("commit failed: \(error.localizedDescription)")
    }
}
print(deletedCount)
"""


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event_matches_rocky_block(
    event: dict[str, Any],
    *,
    calendar_name: str,
    title: str,
    idempotency_key: str,
) -> bool:
    summary = str(event.get("summary") or "")
    description = str(event.get("description") or "")
    calendar = str(event.get("calendar") or "")
    return (
        calendar == calendar_name
        and summary == title
        and "Booked by: Rocky" in description
        and idempotency_key in description
    )


def _query_matching_events(
    *,
    calendar_name: str,
    title: str,
    idempotency_key: str,
    start: str,
    end: str,
    db_path: Path | str | None = None,
    existing_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if existing_events is None:
        start_dt = _parse_iso(start).replace(tzinfo=None) - timedelta(minutes=1)
        end_dt = _parse_iso(end).replace(tzinfo=None) + timedelta(minutes=1)
        existing_events = query_events(
            db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
            start=start_dt,
            end=end_dt,
            include_all_day=False,
        )
    return [
        event
        for event in existing_events
        if _event_matches_rocky_block(
            event,
            calendar_name=calendar_name,
            title=title,
            idempotency_key=idempotency_key,
        )
    ]


def _run_osascript_with_calendar_retry(
    script: str,
    args: list[str],
    *,
    timeout_seconds: int = SCRIPT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = ["osascript", "-e", script, *args]
    try:
        first = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"osascript timed out after {timeout_seconds} seconds",
        )
    if first.returncode == 0:
        return first
    error_text = f"{first.stdout}\n{first.stderr}"
    if "(-600)" not in error_text and "Application isn't running" not in error_text:
        return first
    try:
        subprocess.run(["open", "-a", "Calendar"], capture_output=True, text=True, timeout=timeout_seconds)
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"osascript retry timed out after {timeout_seconds} seconds",
        )


def _run_eventkit_delete(
    *,
    calendar_name: str,
    title: str,
    idempotency_key: str,
    start: str,
    end: str,
    timeout_seconds: int = SCRIPT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = [
        "swift",
        "-e",
        DELETE_EVENTKIT_SCRIPT,
        calendar_name,
        title,
        idempotency_key,
        start,
        end,
    ]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"eventkit delete timed out after {timeout_seconds} seconds",
        )


def _date_args(value: str) -> list[str]:
    dt = _parse_iso(value)
    return [str(dt.year), str(dt.month), str(dt.day), str(dt.hour), str(dt.minute)]


def _record_audit(
    audit_log: AssistantAuditLog,
    *,
    event_type: str,
    idempotency_key: str,
    decision: str,
    reason: str,
    sources: list[Any] | None = None,
    artifacts: dict[str, Any] | None = None,
):
    return audit_log.record_event(
        event_type=event_type,
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision=decision,
        reason=reason,
        sources=sources or [],
        artifacts=artifacts or {},
    )


def _blocked_without_live() -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": "live_flag_required",
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
    }


def _safe_script_artifacts(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def create_calendar_block(
    *,
    kind: str,
    day: str,
    window_start: str,
    window_end: str,
    duration_minutes: int,
    label: str | None = None,
    reason: str = "Rocky live calendar block",
    confidence: str = "medium",
    source_refs: list[Any] | None = None,
    metadata_extra: dict[str, Any] | None = None,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    live: bool = False,
    state_db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    scheduler_db_path: Path | str | None = None,
    db_path: Path | str | None = None,
    existing_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not live:
        return _blocked_without_live()

    sources = list(source_refs or [])
    proposal = build_calendar_dry_run(
        kind=kind,
        day=day,
        window_start=window_start,
        window_end=window_end,
        duration_minutes=duration_minutes,
        label=label,
        reason=reason,
        confidence=confidence,
        source_refs=sources,
        metadata_extra=metadata_extra,
        db_path=db_path,
        existing_events=existing_events,
        ledger_path=ledger_path,
        record_audit=False,
    )
    audit_log = AssistantAuditLog(ledger_path)
    idempotency_key = proposal.get("idempotency_key") or "calendar-write:missing-key"
    if proposal.get("status") != "proposal":
        if proposal.get("reason") == "duplicate_rocky_block":
            state = AssistantCalendarState(state_db_path)
            active = state.get_active(idempotency_key)
            if active:
                matches = _query_matching_events(
                    calendar_name=active["calendar_name"],
                    title=active["title"],
                    idempotency_key=idempotency_key,
                    start=active["start"],
                    end=active["end"],
                    db_path=db_path,
                    existing_events=existing_events,
                )
                if matches:
                    event = _record_audit(
                        audit_log,
                        event_type="calendar.write_requested",
                        idempotency_key=idempotency_key,
                        decision="completed",
                        reason="duplicate_existing_active_event",
                        sources=sources,
                        artifacts={"state": active, "matching_events": len(matches)},
                    )
                    return {
                        **proposal,
                        "status": "skipped_duplicate",
                        "reason": "duplicate_existing_active_event",
                        "audit_id": event.audit_id,
                        "calendar_write_attempted": False,
                        "calendar_event_created": False,
                        "calendar_event_deleted": False,
                        "state": active,
                    }
        failure = _record_audit(
            audit_log,
            event_type="calendar.write_failed",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason=str(proposal.get("reason") or "preflight_blocked"),
            sources=sources,
            artifacts={"proposal": proposal},
        )
        return {
            **proposal,
            "status": "blocked",
            "audit_id": failure.audit_id,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    lock_key = f"calendar_write:{idempotency_key}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=lock_key,
        ttl_seconds=300,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=True,
        metadata={"calendar_name": calendar_name, "kind": kind},
    )
    if not lock.acquired:
        failure = _record_audit(
            audit_log,
            event_type="calendar.write_failed",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="duplicate_calendar_write_lock",
            sources=sources,
            artifacts={"lock": lock.to_dict()},
        )
        return {
            **proposal,
            "status": "blocked",
            "reason": "duplicate_calendar_write_lock",
            "audit_id": failure.audit_id,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    try:
        state = AssistantCalendarState(state_db_path)
        active = state.get_active(idempotency_key)
        if active:
            matches = _query_matching_events(
                calendar_name=active["calendar_name"],
                title=active["title"],
                idempotency_key=idempotency_key,
                start=active["start"],
                end=active["end"],
                db_path=db_path,
                existing_events=existing_events,
            )
            if matches:
                event = _record_audit(
                    audit_log,
                    event_type="calendar.write_requested",
                    idempotency_key=idempotency_key,
                    decision="completed",
                    reason="duplicate_existing_active_event",
                    sources=sources,
                    artifacts={"state": active, "matching_events": len(matches)},
                )
                return {
                    **proposal,
                    "status": "skipped_duplicate",
                    "reason": "duplicate_existing_active_event",
                    "audit_id": event.audit_id,
                    "calendar_write_attempted": False,
                    "calendar_event_created": False,
                    "calendar_event_deleted": False,
                    "state": active,
                }
            stale = state.mark_stale(idempotency_key=idempotency_key)
            failure = _record_audit(
                audit_log,
                event_type="calendar.write_failed",
                idempotency_key=idempotency_key,
                decision="blocked",
                reason="stale_calendar_state_no_matching_event",
                sources=sources,
                artifacts={"stale_state": stale},
            )
            return {
                **proposal,
                "status": "blocked",
                "reason": "stale_calendar_state_no_matching_event",
                "audit_id": failure.audit_id,
                "calendar_write_attempted": False,
                "calendar_event_created": False,
                "calendar_event_deleted": False,
            }

        requested = _record_audit(
            audit_log,
            event_type="calendar.write_requested",
            idempotency_key=idempotency_key,
            decision="allowed",
            reason=reason,
            sources=sources,
            artifacts={
                "calendar_name": calendar_name,
                "title": proposal["title"],
                "start": proposal["start"],
                "end": proposal["end"],
            },
        )
        metadata_description = build_metadata_description(
            kind=kind,
            reason=reason,
            sources=sources,
            confidence=confidence,
            audit_id=requested.audit_id,
            idempotency_key=idempotency_key,
            created_at=requested.created_at,
        )
        result = _run_osascript_with_calendar_retry(
            CREATE_EVENT_SCRIPT,
            [
                calendar_name,
                proposal["title"],
                metadata_description,
                *_date_args(proposal["start"]),
                *_date_args(proposal["end"]),
            ],
        )
        if result.returncode != 0:
            failure = _record_audit(
                audit_log,
                event_type="calendar.write_failed",
                idempotency_key=idempotency_key,
                decision="failed",
                reason="osascript_create_failed",
                sources=sources,
                artifacts=_safe_script_artifacts(result),
            )
            return {
                **proposal,
                "status": "failed",
                "reason": "osascript_create_failed",
                "audit_id": failure.audit_id,
                "calendar_write_attempted": True,
                "calendar_event_created": False,
                "calendar_event_deleted": False,
            }

        event_uid = result.stdout.strip() or None
        matches: list[dict[str, Any]] = []
        for _ in range(VERIFY_ATTEMPTS):
            matches = _query_matching_events(
                calendar_name=calendar_name,
                title=proposal["title"],
                idempotency_key=idempotency_key,
                start=proposal["start"],
                end=proposal["end"],
                db_path=db_path,
            )
            if matches:
                break
            time.sleep(VERIFY_SLEEP_SECONDS)
        if not matches:
            failure = _record_audit(
                audit_log,
                event_type="calendar.write_failed",
                idempotency_key=idempotency_key,
                decision="failed",
                reason="calendar_create_verification_failed",
                sources=sources,
                artifacts={"event_uid": event_uid, "calendar_name": calendar_name},
            )
            return {
                **proposal,
                "status": "failed",
                "reason": "calendar_create_verification_failed",
                "audit_id": failure.audit_id,
                "calendar_write_attempted": True,
                "calendar_event_created": False,
                "calendar_event_deleted": False,
            }

        created = _record_audit(
            audit_log,
            event_type="calendar.event_created",
            idempotency_key=idempotency_key,
            decision="created",
            reason=reason,
            sources=sources,
            artifacts={
                "calendar_name": calendar_name,
                "title": proposal["title"],
                "start": proposal["start"],
                "end": proposal["end"],
                "event_uid": event_uid,
                "verified_matches": len(matches),
            },
        )
        state_row = state.record_created(
            idempotency_key=idempotency_key,
            calendar_name=calendar_name,
            title=proposal["title"],
            start=proposal["start"],
            end=proposal["end"],
            event_uid=event_uid,
            create_audit_id=created.audit_id,
            metadata={
                "kind": kind,
                "confidence": confidence,
                "source_refs": sources,
                "write_requested_audit_id": requested.audit_id,
            },
        )
        return {
            **proposal,
            "status": "created",
            "mode": "live",
            "calendar_name": calendar_name,
            "audit_id": created.audit_id,
            "write_requested_audit_id": requested.audit_id,
            "idempotency_key": idempotency_key,
            "event_uid": event_uid,
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
            "metadata_description": metadata_description,
            "state": state_row,
            "reversal_status": (
                "Delete with calendar-block-delete "
                f"--idempotency-key {idempotency_key} --calendar {calendar_name} --live"
            ),
        }
    finally:
        release_run_lock(
            workflow=WORKFLOW,
            idempotency_key=lock_key,
            db_path=scheduler_db_path,
            ledger_path=ledger_path,
            write_audit=True,
        )


def delete_calendar_block(
    *,
    idempotency_key: str,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    live: bool = False,
    state_db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    scheduler_db_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    if not live:
        return _blocked_without_live()

    audit_log = AssistantAuditLog(ledger_path)
    state = AssistantCalendarState(state_db_path)
    record = state.get_active(idempotency_key)
    if not record:
        event = _record_audit(
            audit_log,
            event_type="calendar.delete_blocked",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="no_active_calendar_state",
            artifacts={"calendar_name": calendar_name},
        )
        return {
            "status": "blocked",
            "reason": "no_active_calendar_state",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    if (
        record["calendar_name"] != calendar_name
        or not str(record["title"]).startswith("Rocky:")
    ):
        event = _record_audit(
            audit_log,
            event_type="calendar.delete_blocked",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="rocky_ownership_check_failed",
            artifacts={"state": record, "requested_calendar": calendar_name},
        )
        return {
            "status": "blocked",
            "reason": "rocky_ownership_check_failed",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    matches = _query_matching_events(
        calendar_name=record["calendar_name"],
        title=record["title"],
        idempotency_key=idempotency_key,
        start=record["start"],
        end=record["end"],
        db_path=db_path,
    )
    if not matches:
        stale = state.mark_stale(idempotency_key=idempotency_key)
        event = _record_audit(
            audit_log,
            event_type="calendar.delete_blocked",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="no_matching_rocky_event_to_delete",
            artifacts={"stale_state": stale},
        )
        return {
            "status": "blocked",
            "reason": "no_matching_rocky_event_to_delete",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    lock_key = f"calendar_delete:{idempotency_key}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=lock_key,
        ttl_seconds=300,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=True,
        metadata={"calendar_name": calendar_name, "operation": "delete"},
    )
    if not lock.acquired:
        event = _record_audit(
            audit_log,
            event_type="calendar.delete_blocked",
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="duplicate_calendar_delete_lock",
            artifacts={"lock": lock.to_dict()},
        )
        return {
            "status": "blocked",
            "reason": "duplicate_calendar_delete_lock",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "calendar_write_attempted": False,
            "calendar_event_created": False,
            "calendar_event_deleted": False,
        }

    try:
        delete_method = "eventkit"
        result = _run_eventkit_delete(
            calendar_name=calendar_name,
            title=record["title"],
            idempotency_key=idempotency_key,
            start=record["start"],
            end=record["end"],
        )
        if result.returncode != 0:
            fallback_result = _run_osascript_with_calendar_retry(
                DELETE_EVENT_SCRIPT,
                [
                    calendar_name,
                    record["title"],
                    record.get("event_uid") or "",
                    idempotency_key,
                ],
            )
            if fallback_result.returncode != 0:
                event = _record_audit(
                    audit_log,
                    event_type="calendar.delete_failed",
                    idempotency_key=idempotency_key,
                    decision="failed",
                    reason="eventkit_delete_failed_and_osascript_fallback_failed",
                    artifacts={
                        "delete_method": "eventkit_then_osascript_fallback",
                        "eventkit": _safe_script_artifacts(result),
                        "osascript": _safe_script_artifacts(fallback_result),
                    },
                )
                return {
                    "status": "failed",
                    "reason": "eventkit_delete_failed_and_osascript_fallback_failed",
                    "audit_id": event.audit_id,
                    "idempotency_key": idempotency_key,
                    "delete_method": "eventkit_then_osascript_fallback",
                    "calendar_write_attempted": True,
                    "calendar_event_created": False,
                    "calendar_event_deleted": False,
                }
            result = fallback_result
            delete_method = "osascript_fallback"

        try:
            deleted_count = int((result.stdout or "0").strip())
        except ValueError:
            deleted_count = 0
        if deleted_count < 1:
            event = _record_audit(
                audit_log,
                event_type="calendar.delete_failed",
                idempotency_key=idempotency_key,
                decision="failed",
                reason="no_calendar_event_deleted",
                artifacts={
                    "delete_method": delete_method,
                    **_safe_script_artifacts(result),
                },
            )
            return {
                "status": "failed",
                "reason": "no_calendar_event_deleted",
                "audit_id": event.audit_id,
                "idempotency_key": idempotency_key,
                "delete_method": delete_method,
                "calendar_write_attempted": True,
                "calendar_event_created": False,
                "calendar_event_deleted": False,
            }

        remaining: list[dict[str, Any]] = []
        for _ in range(VERIFY_ATTEMPTS):
            remaining = _query_matching_events(
                calendar_name=record["calendar_name"],
                title=record["title"],
                idempotency_key=idempotency_key,
                start=record["start"],
                end=record["end"],
                db_path=db_path,
            )
            if not remaining:
                break
            time.sleep(VERIFY_SLEEP_SECONDS)
        if remaining:
            event = _record_audit(
                audit_log,
                event_type="calendar.delete_failed",
                idempotency_key=idempotency_key,
                decision="failed",
                reason="calendar_delete_verification_failed",
                artifacts={
                    "delete_method": delete_method,
                    "remaining_matches": len(remaining),
                    "state": record,
                },
            )
            return {
                "status": "failed",
                "reason": "calendar_delete_verification_failed",
                "audit_id": event.audit_id,
                "idempotency_key": idempotency_key,
                "delete_method": delete_method,
                "calendar_write_attempted": True,
                "calendar_event_created": False,
                "calendar_event_deleted": False,
            }

        event = _record_audit(
            audit_log,
            event_type="calendar.event_deleted",
            idempotency_key=idempotency_key,
            decision="completed",
            reason="rocky_calendar_block_deleted",
            artifacts={
                "calendar_name": calendar_name,
                "title": record["title"],
                "event_uid": record.get("event_uid"),
                "deleted_count": deleted_count,
                "delete_method": delete_method,
            },
        )
        state_row = state.mark_deleted(idempotency_key=idempotency_key, delete_audit_id=event.audit_id)
        return {
            "status": "deleted",
            "reason": "rocky_calendar_block_deleted",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "calendar_name": calendar_name,
            "delete_method": delete_method,
            "calendar_write_attempted": True,
            "calendar_event_created": False,
            "calendar_event_deleted": True,
            "deleted_count": deleted_count,
            "state": state_row,
        }
    finally:
        release_run_lock(
            workflow=WORKFLOW,
            idempotency_key=lock_key,
            db_path=scheduler_db_path,
            ledger_path=ledger_path,
            write_audit=True,
        )
