#!/usr/bin/env python3
"""Read-only TrainingPeaks .ics preview helpers.

Sprint 4.0 deliberately avoids TrainingPeaks credentials, cookies, browser
automation, MCP setup, and calendar writes. This module parses the limited
VEVENT fields Rocky needs for training-block discovery.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TIMEZONE = "Europe/Prague"
DEFAULT_DAYS_AHEAD = 14
MAX_DESCRIPTION_CHARS_READ = 0
SECRET_FIELD_RE = re.compile(r"(cookie|token|secret|password|credential|auth)", re.IGNORECASE)
SPORT_KEYWORDS = {
    "bike": "bike",
    "cycling": "bike",
    "ride": "bike",
    "run": "run",
    "swim": "swim",
    "strength": "strength",
    "mobility": "mobility",
    "yoga": "mobility",
    "walk": "walk",
}


@dataclass(frozen=True)
class TrainingPeaksWorkout:
    source: str
    source_ref: str
    date: str
    planned_start_local: str | None
    planned_end_local: str | None
    planned_duration_minutes: int | None
    title: str
    sport: str | None
    confidence: str
    warnings: list[str] = field(default_factory=list)
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def preview_ics_file(
    path: str | Path,
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    start_date: str | date | None = None,
    source: str = "trainingpeaks_ics_file",
    observed_at: str | None = None,
) -> dict[str, Any]:
    ics_path = Path(path).expanduser()
    if not ics_path.exists():
        return _error_payload("ics_file_missing", source=source)
    text = ics_path.read_text(encoding="utf-8", errors="replace")
    payload = preview_ics_text(
        text,
        days_ahead=days_ahead,
        start_date=start_date,
        source=source,
        observed_at=observed_at,
    )
    payload["input"] = {"type": "ics_file", "path": str(ics_path)}
    return payload


def preview_webcal_url_file(
    path: str | Path,
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    start_date: str | date | None = None,
    timeout_seconds: int = 15,
    observed_at: str | None = None,
) -> dict[str, Any]:
    url_file = Path(path).expanduser()
    warnings: list[str] = []
    if not url_file.exists():
        return _error_payload("webcal_url_file_missing", source="trainingpeaks_webcal")
    warnings.extend(_secret_file_warnings(url_file))
    url = url_file.read_text(encoding="utf-8", errors="replace").strip()
    if not url:
        return _error_payload("webcal_url_file_empty", source="trainingpeaks_webcal")
    fetch_url = _normalise_webcal_url(url)
    try:
        request = Request(fetch_url, headers={"User-Agent": "Rocky TrainingPeaks read-only preview"})
        with urlopen(request, timeout=timeout_seconds) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": "webcal_fetch_failed",
            "safe_error": type(exc).__name__,
            "input": {"type": "webcal_url_file", "path": str(url_file), "url_redacted": True},
            "warnings": warnings,
            "calendar_write_attempted": False,
        }
    payload = preview_ics_text(
        text,
        days_ahead=days_ahead,
        start_date=start_date,
        source="trainingpeaks_webcal",
        observed_at=observed_at,
    )
    payload["input"] = {"type": "webcal_url_file", "path": str(url_file), "url_redacted": True}
    payload["warnings"] = sorted(set(payload.get("warnings", []) + warnings))
    return payload


def preview_ics_text(
    text: str,
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    start_date: str | date | None = None,
    source: str = "trainingpeaks_ics",
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(tz=ZoneInfo(TIMEZONE)).isoformat()
    window_start = _parse_start_date(start_date)
    window_end = window_start + timedelta(days=int(days_ahead))
    events = _parse_vevents(text)
    workouts: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped_count = 0

    for event in events:
        if "RRULE" in event:
            warnings.append("unsupported_recurring_workout")
        workout = _normalise_event(
            event,
            source=source,
            observed_at=observed,
        )
        if workout is None:
            skipped_count += 1
            continue
        workout_day = date.fromisoformat(workout.date)
        if not (window_start <= workout_day < window_end):
            skipped_count += 1
            continue
        workouts.append(workout.to_dict())
        warnings.extend(workout.warnings)

    workouts.sort(key=lambda item: (item["date"], item["planned_start_local"] or "99:99", item["title"]))
    return {
        "status": "ok",
        "source": source,
        "window": {
            "start_date": window_start.isoformat(),
            "days_ahead": int(days_ahead),
            "end_exclusive": window_end.isoformat(),
            "timezone": TIMEZONE,
        },
        "workout_count": len(workouts),
        "skipped_count": skipped_count,
        "workouts": workouts,
        "warnings": sorted(set(warnings)),
        "calendar_write_attempted": False,
    }


def _parse_vevents(text: str) -> list[dict[str, Any]]:
    unfolded = _unfold_ics_lines(text)
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in unfolded:
        if line.upper() == "BEGIN:VEVENT":
            current = {}
            continue
        if line.upper() == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        name, params = _parse_property_name(left)
        if SECRET_FIELD_RE.search(name):
            continue
        current.setdefault(name, []).append({"params": params, "value": _clean_ics_value(value)})
    return events


def _unfold_ics_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        elif raw_line:
            lines.append(raw_line)
    return lines


def _parse_property_name(left: str) -> tuple[str, dict[str, str]]:
    parts = left.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.upper()] = value.strip('"')
    return name, params


def _normalise_event(
    event: dict[str, list[dict[str, Any]]],
    *,
    source: str,
    observed_at: str,
) -> TrainingPeaksWorkout | None:
    dtstart_entry = _first(event, "DTSTART")
    summary = _first_value(event, "SUMMARY") or "Planned workout"
    uid = _first_value(event, "UID")
    categories = _first_value(event, "CATEGORIES")
    warnings: list[str] = []
    if "RRULE" in event:
        warnings.append("unsupported_recurring_workout")
        return None
    if not dtstart_entry:
        return None

    start = _parse_ics_datetime(dtstart_entry)
    end = None
    dtend_entry = _first(event, "DTEND")
    if dtend_entry:
        end = _parse_ics_datetime(dtend_entry)

    duration_minutes = None
    if start and end and isinstance(start, datetime) and isinstance(end, datetime):
        duration_minutes = max(0, round((end - start).total_seconds() / 60))
    elif start and not end:
        duration_entry = _first_value(event, "DURATION")
        if duration_entry:
            duration_delta = _parse_duration(duration_entry)
            if duration_delta is not None and isinstance(start, datetime):
                end = start + duration_delta
                duration_minutes = round(duration_delta.total_seconds() / 60)

    if isinstance(start, date) and not isinstance(start, datetime):
        workout_date = start
        planned_start = None
        planned_end = None
        confidence = "low"
        warnings.append("untimed_or_all_day_workout")
    else:
        assert isinstance(start, datetime)
        workout_date = start.date()
        planned_start = start.isoformat()
        planned_end = end.isoformat() if isinstance(end, datetime) else None
        confidence = "high" if planned_end and duration_minutes else "medium"
        if not planned_end:
            warnings.append("missing_planned_end_time")

    title = _safe_title(summary)
    sport = _derive_sport(title, categories)
    return TrainingPeaksWorkout(
        source=source,
        source_ref=_source_ref(uid=uid, title=title, workout_date=workout_date.isoformat()),
        date=workout_date.isoformat(),
        planned_start_local=planned_start,
        planned_end_local=planned_end,
        planned_duration_minutes=duration_minutes,
        title=title,
        sport=sport,
        confidence=confidence,
        warnings=sorted(set(warnings)),
        observed_at=observed_at,
    )


def _parse_ics_datetime(entry: dict[str, Any]) -> datetime | date | None:
    value = str(entry.get("value") or "")
    params = entry.get("params") or {}
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    tz_name = params.get("TZID") or TIMEZONE
    if value.endswith("Z"):
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo(TIMEZONE))
    parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    try:
        return parsed.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo(TIMEZONE))
    except Exception:
        return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))


def _parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _first(event: dict[str, list[dict[str, Any]]], key: str) -> dict[str, Any] | None:
    values = event.get(key)
    return values[0] if values else None


def _first_value(event: dict[str, list[dict[str, Any]]], key: str) -> str | None:
    item = _first(event, key)
    return str(item.get("value")) if item else None


def _clean_ics_value(value: str) -> str:
    return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def _safe_title(value: str) -> str:
    title = " ".join(value.split())
    if SECRET_FIELD_RE.search(title):
        return "Planned workout"
    return title[:120] or "Planned workout"


def _derive_sport(title: str, categories: str | None) -> str | None:
    haystack = f"{title} {categories or ''}".lower()
    for needle, sport in SPORT_KEYWORDS.items():
        if needle in haystack:
            return sport
    return None


def _source_ref(*, uid: str | None, title: str, workout_date: str) -> str:
    raw = uid or f"{workout_date}:{title}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"trainingpeaks:{digest}"


def _parse_start_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return datetime.now(tz=ZoneInfo(TIMEZONE)).date()


def _normalise_webcal_url(url: str) -> str:
    if url.startswith("webcal://"):
        return "https://" + url.removeprefix("webcal://")
    return url


def _secret_file_warnings(path: Path) -> list[str]:
    warnings: list[str] = []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        warnings.append("webcal_url_file_permissions_too_open")
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if parent_mode & 0o077:
        warnings.append("webcal_url_parent_directory_permissions_too_open")
    return warnings


def _error_payload(reason: str, *, source: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": reason,
        "source": source,
        "workout_count": 0,
        "workouts": [],
        "warnings": [],
        "calendar_write_attempted": False,
    }


def dumps_safe(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


__all__ = [
    "DEFAULT_DAYS_AHEAD",
    "TIMEZONE",
    "TrainingPeaksWorkout",
    "dumps_safe",
    "preview_ics_file",
    "preview_ics_text",
    "preview_webcal_url_file",
]
