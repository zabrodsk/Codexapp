#!/usr/bin/env python3
"""Testable helpers for inspecting macOS LaunchAgents."""
from __future__ import annotations

import os
import plistlib
import hashlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class LaunchAgentSpec:
    label: str
    plist_path: str
    program_arguments: list[str]
    working_directory: str
    stdout_path: str
    stderr_path: str
    weekdays: list[int]
    hour: int
    minute: int
    timezone: str = "Europe/Prague"
    first_expected_run_after: str | None = None

    @property
    def program(self) -> str:
        return self.program_arguments[0] if self.program_arguments else ""


@dataclass
class LaunchCtlStatus:
    loaded: bool
    state: str = "unknown"
    runs: int | None = None
    last_exit_code: int | None = None
    last_exit_raw: str | None = None
    raw: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        raw = str(payload.pop("raw") or "")
        payload["output_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else None
        payload["output_chars"] = len(raw)
        return payload


@dataclass
class LaunchAgentInspection:
    label: str
    status: str
    failure_class: str | None
    issues: list[str] = field(default_factory=list)
    plist: dict[str, Any] = field(default_factory=dict)
    launchctl: dict[str, Any] = field(default_factory=dict)
    previous_expected_run: str | None = None
    next_expected_run: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_plist(path: Path | str) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        return plistlib.load(handle)


def normalize_start_calendar_interval(value: Any) -> list[dict[str, int]]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    normalized: list[dict[str, int]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized.append({str(key): int(val) for key, val in item.items()})
    return sorted(
        normalized,
        key=lambda row: (
            row.get("Weekday", -1),
            row.get("Hour", -1),
            row.get("Minute", -1),
        ),
    )


def expected_calendar_interval(spec: LaunchAgentSpec) -> list[dict[str, int]]:
    return [
        {"Weekday": int(day), "Hour": int(spec.hour), "Minute": int(spec.minute)}
        for day in sorted(spec.weekdays)
    ]


def launchd_weekday(day: datetime) -> int:
    return day.isoweekday() % 7


def expected_run_bounds(spec: LaunchAgentSpec, *, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    tz = ZoneInfo(spec.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    previous: datetime | None = None
    next_run: datetime | None = None
    for offset in range(-14, 15):
        candidate_day = now.date() + timedelta(days=offset)
        candidate = datetime.combine(
            candidate_day,
            datetime.min.time().replace(hour=spec.hour, minute=spec.minute),
            tzinfo=tz,
        )
        if launchd_weekday(candidate) not in set(spec.weekdays):
            continue
        if candidate <= now and (previous is None or candidate > previous):
            previous = candidate
        if candidate > now and (next_run is None or candidate < next_run):
            next_run = candidate
    return previous, next_run


def parse_launchctl_print(text: str, *, returncode: int = 0) -> LaunchCtlStatus:
    if returncode != 0:
        return LaunchCtlStatus(loaded=False, raw=text, error=text.strip() or "launchctl print failed")
    state_match = re.search(r"^\s*state = ([^\n]+)$", text, flags=re.MULTILINE)
    runs_match = re.search(r"^\s*runs = ([0-9]+)$", text, flags=re.MULTILINE)
    last_exit_match = re.search(r"^\s*last exit code = ([^\n]+)$", text, flags=re.MULTILINE)
    last_exit_raw = last_exit_match.group(1).strip() if last_exit_match else None
    last_exit_code = None
    if last_exit_raw and last_exit_raw.isdigit():
        last_exit_code = int(last_exit_raw)
    return LaunchCtlStatus(
        loaded=True,
        state=state_match.group(1).strip() if state_match else "unknown",
        runs=int(runs_match.group(1)) if runs_match else None,
        last_exit_code=last_exit_code,
        last_exit_raw=last_exit_raw,
        raw=text,
    )


def read_launchctl_status(label: str) -> LaunchCtlStatus:
    uid = os.getuid()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{label}"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    return parse_launchctl_print(text, returncode=proc.returncode)


def inspect_launchagent(
    spec: LaunchAgentSpec,
    *,
    now: datetime | None = None,
    launchctl_text: str | None = None,
    launchctl_returncode: int = 0,
    read_launchctl: bool = True,
) -> LaunchAgentInspection:
    issues: list[str] = []
    failure_class: str | None = None
    plist_path = Path(spec.plist_path)
    plist: dict[str, Any] = {}

    if not plist_path.exists():
        issues.append("launchagent_plist_missing")
        failure_class = "launchagent_plist_missing"
    else:
        try:
            plist = load_plist(plist_path)
        except Exception as exc:
            issues.append(f"launchagent_plist_unreadable:{exc}")
            failure_class = "launchagent_plist_missing"

    if plist:
        if plist.get("Label") != spec.label:
            issues.append("launchagent_label_mismatch")
            failure_class = failure_class or "launchagent_program_mismatch"
        if list(plist.get("ProgramArguments") or []) != spec.program_arguments:
            issues.append("launchagent_program_mismatch")
            failure_class = failure_class or "launchagent_program_mismatch"
        if str(plist.get("WorkingDirectory") or "") != spec.working_directory:
            issues.append("launchagent_working_directory_mismatch")
            failure_class = failure_class or "launchagent_program_mismatch"
        if str(plist.get("StandardOutPath") or "") != spec.stdout_path:
            issues.append("launchagent_stdout_path_mismatch")
        if str(plist.get("StandardErrorPath") or "") != spec.stderr_path:
            issues.append("launchagent_stderr_path_mismatch")
        actual_schedule = normalize_start_calendar_interval(plist.get("StartCalendarInterval"))
        expected_schedule = expected_calendar_interval(spec)
        if actual_schedule != expected_schedule:
            issues.append("launchagent_schedule_mismatch")
            failure_class = failure_class or "launchagent_schedule_mismatch"

    if len(spec.program_arguments) > 1:
        helper_path = Path(spec.program_arguments[1])
        if helper_path.is_absolute() and not helper_path.exists():
            issues.append("helper_missing")
            failure_class = failure_class or "helper_missing"

    if launchctl_text is not None:
        launchctl_status = parse_launchctl_print(launchctl_text, returncode=launchctl_returncode)
    elif read_launchctl:
        try:
            launchctl_status = read_launchctl_status(spec.label)
        except Exception as exc:
            launchctl_status = LaunchCtlStatus(loaded=False, error=str(exc), raw=str(exc))
    else:
        launchctl_status = LaunchCtlStatus(loaded=True, state="not checked")

    if not launchctl_status.loaded:
        issues.append("launchagent_not_loaded")
        failure_class = failure_class or "launchagent_not_loaded"
    if launchctl_status.last_exit_code not in (None, 0):
        issues.append("launchagent_nonzero_exit")
        failure_class = failure_class or "launchagent_nonzero_exit"

    previous_run, next_run = expected_run_bounds(spec, now=now)
    status = "healthy"
    if failure_class:
        status = "blocked"
    elif any(issue.endswith("_mismatch") for issue in issues):
        status = "degraded"

    return LaunchAgentInspection(
        label=spec.label,
        status=status,
        failure_class=failure_class,
        issues=issues,
        plist=plist,
        launchctl=launchctl_status.to_dict(),
        previous_expected_run=previous_run.isoformat() if previous_run else None,
        next_expected_run=next_run.isoformat() if next_run else None,
    )
