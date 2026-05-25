#!/usr/bin/env python3
"""Read-only weekly calendar hygiene checks for Rocky-owned blocks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from assistant_audit_log import AssistantAuditLog
from assistant_calendar_policy import POLICY_VERSION
from assistant_calendar_state import AssistantCalendarState

TIMEZONE = "Europe/Prague"
WORKFLOW = "weekly_calendar_hygiene"
ROCKY_PREFIX = "Rocky:"
IDEMPOTENCY_RE = re.compile(r"rocky:[A-Za-z0-9_:-]+")
SENSITIVE_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def inspect_weekly_calendar_hygiene(
    *,
    start_date: str | date | None = None,
    days: int = 14,
    calendar_name: str = "Calendar",
    mark_stale: bool = False,
    live: bool = False,
    state_db_path: str | Path | None = None,
    db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    events: list[dict[str, Any]] | None = None,
    state_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    start_day = _parse_date(start_date) if start_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    day_count = max(1, min(31, int(days)))
    end_day = start_day + timedelta(days=day_count)
    if mark_stale and not live:
        return _redact_payload({
            "status": "blocked",
            "reason": "live_flag_required_for_mark_stale",
            "start_date": start_day.isoformat(),
            "days": day_count,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
            "state_mutated": False,
        })

    errors: list[dict[str, Any]] = []
    if events is None:
        try:
            events = query_events(
                db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
                start=datetime.combine(start_day, time(0, 0)),
                end=datetime.combine(end_day, time(0, 0)),
                include_all_day=True,
            )
        except Exception as exc:
            events = []
            errors.append({"source": "calendar", "reason": "calendar_read_failed", "error_hash": _hash_text(str(exc))})
    state = AssistantCalendarState(state_db_path)
    if state_blocks is None:
        try:
            state_blocks = state.list_blocks(calendar_name=calendar_name, limit=500)
        except Exception as exc:
            state_blocks = []
            errors.append({"source": "calendar_state", "reason": "calendar_state_read_failed", "error_hash": _hash_text(str(exc))})

    sanitized_events = [_safe_event(event) for event in events or []]
    rocky_events = [event for event in sanitized_events if _is_rocky_owned_event(event)]
    active_state = [row for row in state_blocks or [] if str(row.get("status") or "") == "active"]
    state_by_key = {str(row.get("idempotency_key")): row for row in state_blocks or [] if row.get("idempotency_key")}
    active_by_key = {str(row.get("idempotency_key")): row for row in active_state if row.get("idempotency_key")}

    duplicate_blocks = _duplicate_rocky_events(rocky_events)
    weekend_violations = [event for event in rocky_events if _event_weekday(event) >= 4]
    missing_metadata = [event for event in rocky_events if not event.get("has_rocky_metadata") or not event.get("idempotency_key")]
    orphan_events = [event for event in rocky_events if event.get("idempotency_key") and event.get("idempotency_key") not in state_by_key]
    stale_candidates = _stale_state_candidates(active_state, rocky_events, start_day=start_day, end_day=end_day)
    day_hygiene = _day_hygiene(sanitized_events, start_day=start_day, days=day_count)
    overbooked_days = [day for day in day_hygiene if day.get("status") == "overloaded"]
    no_focus_days = [day for day in day_hygiene if day.get("max_focus_window_minutes", 0) < 60 and _parse_date(day["date"]).weekday() < 5]

    marked_stale: list[str] = []
    if mark_stale and live:
        audit = AssistantAuditLog(ledger_path)
        for item in stale_candidates:
            key = str(item.get("idempotency_key") or "")
            if key and key in active_by_key:
                stale = state.mark_stale(idempotency_key=key)
                marked_stale.append(key)
                audit.record_event(
                    event_type="calendar.state_marked_stale",
                    workflow=WORKFLOW,
                    idempotency_key=key,
                    policy_version=POLICY_VERSION,
                    decision="recovered",
                    reason="weekly_hygiene_active_state_missing_calendar_event",
                    artifacts={"state": _safe_state_row(stale or {})},
                )

    issue_count = sum(len(items) for items in [duplicate_blocks, weekend_violations, missing_metadata, orphan_events, stale_candidates, overbooked_days, no_focus_days])
    status = "degraded" if errors else "manual_review_required" if issue_count else "ok"
    return _redact_payload({
        "status": status,
        "reason": "weekly_calendar_hygiene_issues_found" if issue_count else "weekly_calendar_hygiene_clean",
        "start_date": start_day.isoformat(),
        "end_date": (end_day - timedelta(days=1)).isoformat(),
        "days": day_count,
        "calendar_name": calendar_name,
        "issue_count": issue_count,
        "duplicate_rocky_blocks": duplicate_blocks,
        "stale_state_candidates": stale_candidates,
        "orphan_rocky_events": orphan_events,
        "missing_metadata_events": missing_metadata,
        "weekend_policy_violations": weekend_violations,
        "overbooked_days": overbooked_days,
        "no_realistic_focus_days": no_focus_days,
        "day_hygiene": day_hygiene,
        "marked_stale": marked_stale,
        "state_mutated": bool(marked_stale),
        "errors": errors,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def _duplicate_rocky_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        day = str(event.get("start_local") or "")[:10]
        key = (str(event.get("summary") or ""), day, str(event.get("idempotency_key") or ""))
        groups[key].append(event)
    duplicates = []
    for (summary, day, key), items in groups.items():
        if len(items) > 1:
            duplicates.append({"summary": summary, "date": day, "idempotency_key": key, "count": len(items), "matches": items[:4]})
    return duplicates


def _stale_state_candidates(records: list[dict[str, Any]], events: list[dict[str, Any]], *, start_day: date, end_day: date) -> list[dict[str, Any]]:
    event_keys = {str(event.get("idempotency_key") or "") for event in events if event.get("idempotency_key")}
    result = []
    for row in records:
        key = str(row.get("idempotency_key") or "")
        start = _parse_state_dt(row.get("start"))
        if start and not (start_day <= start.date() < end_day):
            continue
        if key and key not in event_keys:
            result.append(_safe_state_row(row))
    return result


def _is_rocky_owned_event(event: dict[str, Any]) -> bool:
    return (
        str(event.get("summary") or "").startswith(ROCKY_PREFIX)
        or bool(event.get("idempotency_key"))
        or bool(event.get("has_rocky_metadata"))
    )


def _day_hygiene(events: list[dict[str, Any]], *, start_day: date, days: int) -> list[dict[str, Any]]:
    result = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        day_events = [event for event in events if str(event.get("start_local") or "").startswith(day.isoformat())]
        busy = []
        for event in day_events:
            if event.get("all_day"):
                continue
            start = _parse_event_dt(event.get("start_local"))
            end = _parse_event_dt(event.get("end_local"))
            if not start or not end:
                continue
            window_start = datetime.combine(day, time(12, 30))
            window_end = datetime.combine(day, time(19, 30))
            if end <= window_start or start >= window_end:
                continue
            busy.append((max(start, window_start), min(end, window_end)))
        busy.sort()
        free_windows = []
        cursor = datetime.combine(day, time(12, 30))
        end_window = datetime.combine(day, time(19, 30))
        for start, end in busy:
            if start > cursor:
                minutes = int((start - cursor).total_seconds() // 60)
                free_windows.append(minutes)
            if end > cursor:
                cursor = end
        if cursor < end_window:
            free_windows.append(int((end_window - cursor).total_seconds() // 60))
        max_focus = max(free_windows or [0])
        free_after_noon = sum(free_windows)
        status = "overloaded" if free_after_noon < 60 and day.weekday() < 5 else "ok"
        result.append({"date": day.isoformat(), "event_count": len(day_events), "free_minutes_after_noon": free_after_noon, "max_focus_window_minutes": max_focus, "status": status})
    return result


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    description = str(event.get("description") or "")
    key = _extract_idempotency(description)
    return {
        "summary": _safe_text(event.get("summary"), 160),
        "start_local": event.get("start_local"),
        "end_local": event.get("end_local"),
        "all_day": bool(event.get("all_day")),
        "calendar": _safe_text(event.get("calendar"), 80),
        "has_rocky_metadata": "Booked by: Rocky" in description,
        "idempotency_key": key,
        "description_sha256": _hash_text(description) if description else None,
        "description_chars": len(description),
    }


def _safe_state_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _safe_text(row.get(key), 200) if isinstance(row.get(key), str) else row.get(key) for key in ("idempotency_key", "calendar_name", "title", "start", "end", "status", "updated_at")}


def _extract_idempotency(description: str) -> str | None:
    match = IDEMPOTENCY_RE.search(description or "")
    return match.group(0) if match else None


def _event_weekday(event: dict[str, Any]) -> int:
    dt = _parse_event_dt(event.get("start_local"))
    return dt.weekday() if dt else 0


def _parse_event_dt(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_state_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _safe_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 800)
    return value


def _hash_text(value: Any) -> str:
    safe = SENSITIVE_RE.sub("[redacted]", str(value or ""))
    return hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect Rocky weekly calendar hygiene without editing Apple Calendar.")
    parser.add_argument("--start-date", dest="start_date")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--calendar", default="Calendar", dest="calendar_name")
    parser.add_argument("--mark-stale", action="store_true", dest="mark_stale")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--state-db", dest="state_db")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = inspect_weekly_calendar_hygiene(start_date=args.start_date, days=args.days, calendar_name=args.calendar_name, mark_stale=args.mark_stale, live=args.live, state_db_path=args.state_db, db_path=args.db_path, ledger_path=args.ledger_path)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else f"Weekly calendar hygiene: {payload.get('status')} issues={payload.get('issue_count', 0)}")
    return 0 if payload.get("status") in {"ok", "manual_review_required"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
