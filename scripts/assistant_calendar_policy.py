#!/usr/bin/env python3
"""Calendar booking policy evaluation for Rocky assistant dry-runs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Any


POLICY_VERSION = "rocky-calendar-policy-v1"
TIMEZONE = "Europe/Prague"
ALLOWED_WEEKDAYS = {0, 1, 2, 3}
BLOCKED_WEEKDAYS = {4, 5, 6}
VALID_BLOCK_TYPES = {"training", "email_triage", "coding_focus", "task_focus"}
TITLE_PREFIX_BY_KIND = {
    "training": "Rocky: Training",
    "email_triage": "Rocky: Email triage",
    "coding_focus": "Rocky: Coding focus",
    "task_focus": "Rocky: Task focus",
}


@dataclass
class CalendarPolicyDecision:
    allowed: bool
    decision: str
    kind: str
    date: str
    start: str
    end: str
    duration_minutes: int
    policy_version: str
    timezone: str
    idempotency_key: str
    reasons: list[str] = field(default_factory=list)
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def parse_hhmm(value: str | time) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    return datetime.strptime(str(value), "%H:%M").time()


def combine_local(day: str | date, clock: str | time) -> datetime:
    parsed_day = parse_date(day)
    parsed_clock = parse_hhmm(clock)
    return datetime.combine(parsed_day, parsed_clock, tzinfo=ZoneInfo(TIMEZONE))


def stable_idempotency_key(
    *,
    kind: str,
    day: str | date,
    start: str | time,
    duration_minutes: int,
    source_refs: list[Any] | None = None,
    policy_version: str = POLICY_VERSION,
) -> str:
    start_dt = combine_local(day, start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    payload = {
        "kind": kind,
        "date": start_dt.date().isoformat(),
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "duration_minutes": int(duration_minutes),
        "source_refs": source_refs or [],
        "policy_version": policy_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"rocky:{kind}:{start_dt.date().isoformat()}:{digest}"


def title_for_kind(kind: str, label: str | None = None) -> str:
    label = " ".join(str(label or "").split())
    if kind == "training":
        suffix = label or "planned workout"
        return f"Rocky: Training - {suffix}"
    if kind == "email_triage":
        return "Rocky: Email triage - unread attention"
    if kind == "coding_focus":
        suffix = label or "project"
        return f"Rocky: Coding focus - {suffix}"
    if kind == "task_focus":
        suffix = label or "task"
        return f"Rocky: Task focus - {suffix}"
    return f"Rocky: {kind}"


def evaluate_calendar_policy(
    *,
    kind: str,
    day: str | date,
    start: str | time,
    duration_minutes: int,
    source_refs: list[Any] | None = None,
    label: str | None = None,
) -> CalendarPolicyDecision:
    reasons: list[str] = []
    parsed_day = parse_date(day)
    parsed_start = parse_hhmm(start)
    duration_minutes = int(duration_minutes)
    start_dt = combine_local(parsed_day, parsed_start)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    if kind not in VALID_BLOCK_TYPES:
        reasons.append("unsupported_block_type")
    if parsed_day.weekday() in BLOCKED_WEEKDAYS:
        reasons.append("proactive_booking_blocked_on_friday_saturday_sunday")
    if parsed_day.weekday() not in ALLOWED_WEEKDAYS:
        reasons.append("proactive_booking_not_monday_through_thursday")
    if duration_minutes <= 0:
        reasons.append("duration_must_be_positive")
    if kind == "email_triage" and duration_minutes < 30:
        reasons.append("email_triage_minimum_duration_is_30_minutes")
    if kind == "email_triage" and parsed_start < time(12, 0):
        reasons.append("email_triage_must_start_no_earlier_than_noon")
    if kind == "coding_focus" and duration_minutes < 60:
        reasons.append("coding_focus_minimum_duration_is_60_minutes")
    if kind == "coding_focus" and end_dt.time() > time(19, 30):
        reasons.append("coding_focus_must_end_by_19_30")
    if kind == "task_focus" and end_dt.time() > time(19, 30):
        reasons.append("task_focus_must_end_by_19_30")

    idempotency_key = stable_idempotency_key(
        kind=kind,
        day=parsed_day,
        start=parsed_start,
        duration_minutes=duration_minutes,
        source_refs=source_refs,
    )
    allowed = not reasons
    if allowed:
        reasons.append("policy_allowed")
    return CalendarPolicyDecision(
        allowed=allowed,
        decision="allowed" if allowed else "blocked",
        kind=kind,
        date=parsed_day.isoformat(),
        start=start_dt.strftime("%H:%M"),
        end=end_dt.strftime("%H:%M"),
        duration_minutes=duration_minutes,
        policy_version=POLICY_VERSION,
        timezone=TIMEZONE,
        idempotency_key=idempotency_key,
        reasons=reasons,
        title=title_for_kind(kind, label=label),
    )
