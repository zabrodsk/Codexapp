#!/usr/bin/env python3
"""Read-only status and reconciliation helpers for Rocky calendar blocks."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_audit_log import AssistantAuditLog
from assistant_calendar_policy import POLICY_VERSION
from assistant_calendar_state import AssistantCalendarState


WORKFLOW = "calendar_status"
SCRIPT_TIMEOUT_SECONDS = 10


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_loads(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _state_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "idempotency_key": row.get("idempotency_key"),
        "calendar_name": row.get("calendar_name"),
        "title": row.get("title"),
        "start": row.get("start"),
        "end": row.get("end"),
        "event_uid": row.get("event_uid"),
        "status": row.get("status"),
        "create_audit_id": row.get("create_audit_id"),
        "delete_audit_id": row.get("delete_audit_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "deleted_at": row.get("deleted_at"),
        "metadata": _json_loads(row.get("metadata_json")),
    }


def _notes_fingerprint(notes: str) -> dict[str, Any]:
    return {
        "notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest()[:16],
        "notes_chars": len(notes),
    }


def sanitize_event(event: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    notes = str(event.get("description") or "")
    return {
        "summary": str(event.get("summary") or ""),
        "start_local": event.get("start_local"),
        "end_local": event.get("end_local"),
        "calendar": str(event.get("calendar") or ""),
        "all_day": bool(event.get("all_day")),
        "has_rocky_metadata": "Booked by: Rocky" in notes,
        "has_idempotency_key": idempotency_key in notes,
        **_notes_fingerprint(notes),
    }


def _matches_rocky_event(
    event: dict[str, Any],
    *,
    calendar_name: str,
    title: str,
    idempotency_key: str,
) -> bool:
    notes = str(event.get("description") or "")
    return (
        str(event.get("calendar") or "") == calendar_name
        and str(event.get("summary") or "") == title
        and "Booked by: Rocky" in notes
        and idempotency_key in notes
    )


def _query_record_matches(
    record: dict[str, Any],
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    start = _parse_iso(record["start"]).replace(tzinfo=None) - timedelta(minutes=1)
    end = _parse_iso(record["end"]).replace(tzinfo=None) + timedelta(minutes=1)
    events = query_events(
        db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
        start=start,
        end=end,
        include_all_day=False,
    )
    return [
        event
        for event in events
        if _matches_rocky_event(
            event,
            calendar_name=record["calendar_name"],
            title=record["title"],
            idempotency_key=record["idempotency_key"],
        )
    ]


def _status_for_record(
    record: dict[str, Any],
    *,
    requested_calendar_name: str,
    matches: list[dict[str, Any]],
) -> tuple[str, str]:
    state_status = str(record.get("status") or "")
    if record.get("calendar_name") != requested_calendar_name:
        return (
            "calendar_mismatch",
            "Use the calendar name stored in Rocky state or inspect the state row before acting.",
        )
    if state_status == "active" and matches:
        return ("active_verified", "No action needed.")
    if state_status == "active" and not matches:
        return (
            "stale_state_candidate",
            "Run calendar-block-reconcile --mark-stale after confirming the event is absent.",
        )
    if state_status == "deleted" and matches:
        return (
            "orphan_calendar_event",
            "Manually inspect Apple Calendar; Sprint 3.1 does not force-delete orphan events.",
        )
    if state_status == "deleted" and not matches:
        return ("deleted_verified", "No action needed.")
    if state_status == "stale" and matches:
        return (
            "calendar_mismatch",
            "Rocky state is stale but the event exists; inspect before changing state.",
        )
    return (
        "stale_state_candidate",
        "State is not active/deleted or the Calendar event is missing; inspect before booking.",
    )


def inspect_calendar_block(
    *,
    idempotency_key: str,
    calendar_name: str = "Calendar",
    state_db_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    state = AssistantCalendarState(state_db_path)
    record = state.get(idempotency_key)
    if not record:
        return {
            "status": "state_missing",
            "idempotency_key": idempotency_key,
            "state": None,
            "calendar_match_count": 0,
            "calendar_matches": [],
            "recommended_action": "No Rocky state row exists for this idempotency key.",
        }

    matches = _query_record_matches(record, db_path=db_path)
    status, action = _status_for_record(
        record,
        requested_calendar_name=calendar_name,
        matches=matches,
    )
    return {
        "status": status,
        "idempotency_key": idempotency_key,
        "state": _state_summary(record),
        "calendar_match_count": len(matches),
        "calendar_matches": [
            sanitize_event(event, idempotency_key=idempotency_key)
            for event in matches
        ],
        "recommended_action": action,
    }


def reconcile_calendar_blocks(
    *,
    calendar_name: str = "Calendar",
    mark_stale: bool = False,
    state_db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    state = AssistantCalendarState(state_db_path)
    records = state.list_blocks(calendar_name=calendar_name)
    audit_log = AssistantAuditLog(ledger_path)
    results: list[dict[str, Any]] = []
    marked_stale: list[str] = []

    for record in records:
        payload = inspect_calendar_block(
            idempotency_key=record["idempotency_key"],
            calendar_name=calendar_name,
            state_db_path=state_db_path,
            db_path=db_path,
        )
        if mark_stale and payload["status"] == "stale_state_candidate" and record["status"] == "active":
            stale = state.mark_stale(idempotency_key=record["idempotency_key"])
            audit_log.record_event(
                event_type="calendar.state_marked_stale",
                workflow=WORKFLOW,
                idempotency_key=record["idempotency_key"],
                policy_version=POLICY_VERSION,
                decision="recovered",
                reason="active_state_missing_calendar_event",
                artifacts={"state": _state_summary(stale)},
            )
            payload["state"] = _state_summary(stale)
            payload["state_mutated"] = True
            marked_stale.append(record["idempotency_key"])
        else:
            payload["state_mutated"] = False
        results.append(payload)

    if mark_stale:
        audit_log.record_event(
            event_type="calendar.state_reconciled",
            workflow=WORKFLOW,
            idempotency_key=f"calendar-reconcile:{calendar_name}",
            policy_version=POLICY_VERSION,
            decision="completed",
            reason="calendar_state_reconcile_completed",
            artifacts={
                "calendar_name": calendar_name,
                "checked_count": len(results),
                "marked_stale_count": len(marked_stale),
                "marked_stale_keys": marked_stale,
            },
        )

    return {
        "status": "ok",
        "calendar_name": calendar_name,
        "checked_count": len(results),
        "marked_stale_count": len(marked_stale),
        "mark_stale": mark_stale,
        "blocks": results,
    }


def _run_command(command: list[str], *, timeout_seconds: int = SCRIPT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"timed out after {timeout_seconds} seconds",
        )


def _eventkit_label(raw_value: str) -> str:
    labels = {
        "0": "not_determined",
        "1": "restricted",
        "2": "denied",
        "3": "authorized",
        "4": "full_access_or_write_only",
    }
    return labels.get(raw_value.strip(), "unknown")


def calendar_write_health(
    *,
    db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    write_audit: bool = True,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    calendar_db_path = Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH

    if calendar_db_path.exists():
        try:
            now = datetime.now()
            sample = query_events(
                db_path=calendar_db_path,
                start=now,
                end=now + timedelta(minutes=1),
                include_all_day=False,
            )
            checks["calendar_db"] = {
                "status": "ok",
                "path": str(calendar_db_path),
                "sample_event_count": len(sample),
            }
        except Exception as exc:  # pragma: no cover - exact sqlite exceptions vary by host.
            checks["calendar_db"] = {
                "status": "blocked",
                "path": str(calendar_db_path),
                "error": str(exc),
            }
    else:
        checks["calendar_db"] = {
            "status": "blocked",
            "path": str(calendar_db_path),
            "error": "calendar_db_missing",
        }

    swift_path = shutil.which("swift")
    checks["swift"] = {
        "status": "ok" if swift_path else "blocked",
        "path": swift_path,
    }

    if swift_path:
        eventkit = _run_command([
            swift_path,
            "-e",
            "import EventKit; print(EKEventStore.authorizationStatus(for: .event).rawValue)",
        ])
        raw = (eventkit.stdout or "").strip()
        checks["eventkit"] = {
            "status": "ok" if eventkit.returncode == 0 and raw in {"3", "4"} else "blocked",
            "authorization_raw": raw,
            "authorization": _eventkit_label(raw),
            "returncode": eventkit.returncode,
            "stderr": eventkit.stderr,
        }
    else:
        checks["eventkit"] = {
            "status": "blocked",
            "authorization": "swift_missing",
        }

    applescript = _run_command([
        "osascript",
        "-e",
        'tell application "Calendar" to get name of calendars',
    ])
    calendar_names = [
        item.strip()
        for item in (applescript.stdout or "").replace(",", "\n").splitlines()
        if item.strip()
    ]
    checks["applescript_calendar"] = {
        "status": "ok" if applescript.returncode == 0 else "blocked",
        "returncode": applescript.returncode,
        "calendar_count": len(calendar_names),
        "stderr": applescript.stderr,
    }

    blocked = [name for name, check in checks.items() if check.get("status") != "ok"]
    status = "ok" if not blocked else "blocked"
    payload = {
        "status": status,
        "blocked_checks": blocked,
        "checks": checks,
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
    }

    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="calendar.write_health_checked",
            workflow=WORKFLOW,
            idempotency_key="calendar-write-health",
            policy_version=POLICY_VERSION,
            decision="allowed" if status == "ok" else "blocked",
            reason="calendar_write_health_ok" if status == "ok" else "calendar_write_health_blocked",
            artifacts=payload,
        )
        payload["audit_id"] = event.audit_id

    return payload
