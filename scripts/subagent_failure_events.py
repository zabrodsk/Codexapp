#!/usr/bin/env python3
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rocky_run_ledger import normalize_status


DEFAULT_FAILURE_LEDGER_PATH = (
    Path(__file__).resolve().parents[1] / "improvement" / "subagent_failures.jsonl"
)

VALIDATION_CODE_CHANGED_ONLY = "code_changed_only"
VALIDATION_TESTS_PASSED = "tests_passed"
VALIDATION_LIVE_PATH_EXERCISED = "live_path_exercised"
VALIDATION_OUTCOME_CONFIRMED = "outcome_confirmed"
VALIDATION_NOT_VERIFIED = "not_verified"
VALIDATION_LEVELS = {
    VALIDATION_CODE_CHANGED_ONLY,
    VALIDATION_TESTS_PASSED,
    VALIDATION_LIVE_PATH_EXERCISED,
    VALIDATION_OUTCOME_CONFIRMED,
    VALIDATION_NOT_VERIFIED,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_validation_result(
    level: str,
    *,
    evidence: list[str] | None = None,
    not_verified: list[str] | None = None,
) -> dict[str, Any]:
    if level not in VALIDATION_LEVELS:
        raise ValueError(f"Unknown validation level: {level}")
    return {
        "level": level,
        "evidence": list(evidence or []),
        "not_verified": list(not_verified or []),
    }


def default_validation_result() -> dict[str, Any]:
    return build_validation_result(
        VALIDATION_NOT_VERIFIED,
        not_verified=["No validation evidence was recorded for this event."],
    )


def validation_level(event: "SubagentFailureEvent") -> str:
    result = event.validation_result or {}
    level = str(result.get("level") or VALIDATION_NOT_VERIFIED)
    return level if level in VALIDATION_LEVELS else VALIDATION_NOT_VERIFIED


def validation_outcome_confirmed(event: "SubagentFailureEvent") -> bool:
    return validation_level(event) == VALIDATION_OUTCOME_CONFIRMED


def render_validation_result(event: "SubagentFailureEvent") -> str:
    result = event.validation_result or default_validation_result()
    level = validation_level(event)
    lines = [f"Validation level: {level}."]
    evidence = [str(item) for item in result.get("evidence") or [] if str(item).strip()]
    missing = [str(item) for item in result.get("not_verified") or [] if str(item).strip()]
    if evidence:
        lines.append(f"Evidence: {_clip('; '.join(evidence), 260)}")
    if missing:
        lines.append(f"Not verified: {_clip('; '.join(missing), 260)}")
    return " ".join(lines)


@dataclass
class SubagentFailureEvent:
    event_id: str
    agent: str
    job_name: str
    run_id: str | None
    session_id: str | None = None
    session_key: str | None = None
    status: str = "error"
    error_class: str | None = None
    error_message: str | None = None
    blocked_dependency: str | None = None
    suggested_next_action: str | None = None
    delivery_channel: str | None = None
    recoverable: bool = False
    retry_recommended: bool = False
    source_kind: str = "manual"
    timestamp: str = field(default_factory=utc_now_iso)
    human_summary: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    recovery_status: str = "unresolved"
    recovery_attempts: int = 0
    last_recovery_error: str | None = None
    general_reported_at: str | None = None
    general_reported_status: str | None = None
    resolved_at: str | None = None
    validation_result: dict[str, Any] = field(default_factory=default_validation_result)


def classify_error(
    error_message: str | None,
    *,
    local_completed: bool = False,
    delivery_attempted: bool = False,
) -> tuple[str, str | None, str | None]:
    return normalize_status(
        error_message,
        local_completed=local_completed,
        delivery_attempted=delivery_attempted,
        delivery_succeeded=False,
    )


def build_failure_event(
    *,
    agent: str,
    job_name: str,
    run_id: str | None = None,
    session_id: str | None = None,
    session_key: str | None = None,
    status: str = "error",
    error_class: str | None = None,
    error_message: str | None = None,
    blocked_dependency: str | None = None,
    suggested_next_action: str | None = None,
    delivery_channel: str | None = None,
    recoverable: bool = False,
    retry_recommended: bool = False,
    source_kind: str = "manual",
    human_summary: str | None = None,
    artifacts: dict[str, Any] | None = None,
) -> SubagentFailureEvent:
    if error_class is None and error_message:
        _, _, error_class = classify_error(error_message)
    return SubagentFailureEvent(
        event_id=str(uuid.uuid4())[:12],
        agent=agent,
        job_name=job_name,
        run_id=run_id,
        session_id=session_id,
        session_key=session_key,
        status=status,
        error_class=error_class,
        error_message=error_message,
        blocked_dependency=blocked_dependency,
        suggested_next_action=suggested_next_action,
        delivery_channel=delivery_channel,
        recoverable=recoverable,
        retry_recommended=retry_recommended,
        source_kind=source_kind,
        human_summary=human_summary,
        artifacts=artifacts or {},
    )


def render_native_notice(event: SubagentFailureEvent) -> str:
    headline = f"[{event.agent.upper()}_{event.status.upper()}] {event.job_name} hit a blocker."
    lines = [headline]
    if event.human_summary:
        lines.append(event.human_summary)
    elif event.error_message:
        lines.append(_clip(event.error_message, 220))
    if event.recoverable:
        lines.append("Rocky has a structured recovery signal and can attempt safe recovery.")
    elif event.suggested_next_action:
        lines.append(f"Next action: {event.suggested_next_action}")
    return "\n".join(lines)


def render_general_report(event: SubagentFailureEvent) -> str:
    if event.recovery_status == "resolved" and validation_outcome_confirmed(event):
        status_line = "Current status: intended outcome confirmed."
    elif event.recovery_status == "resolved":
        status_line = "Current status: legacy resolved marker; intended outcome not confirmed by validation evidence."
    else:
        status_line = {
            "local_recovery_succeeded": "Current status: recovery succeeded locally; intended outcome not confirmed.",
            "human_required": "Current status: human action required.",
            "unrecoverable": "Current status: still blocked; no safe auto-recovery rule matched.",
        }.get(event.recovery_status, "Current status: still under Rocky follow-up.")

    lines = [
        f"Subagent incident: {event.agent} / {event.job_name}",
        f"What failed: {event.status}{f' ({event.error_class})' if event.error_class else ''}.",
    ]
    if event.error_message:
        lines.append(f"Details: {_clip(event.error_message, 260)}")
    if event.recovery_attempts:
        if event.last_recovery_error:
            lines.append(f"Rocky tried recovery {event.recovery_attempts} time(s): {_clip(event.last_recovery_error, 220)}")
        else:
            lines.append(f"Rocky tried recovery {event.recovery_attempts} time(s).")
    else:
        lines.append("Rocky did not run an automatic recovery step.")
    lines.append(render_validation_result(event))
    lines.append(status_line)
    if event.delivery_channel:
        lines.append(f"Native channel: {event.delivery_channel}")
    if event.suggested_next_action and event.recovery_status != "resolved":
        lines.append(f"Human action: {event.suggested_next_action}")
    lines.append(f"Event id: {event.event_id}")
    return "\n".join(lines)


class SubagentFailureLedger:
    def __init__(self, ledger_path: Path | None = None):
        self.path = ledger_path or DEFAULT_FAILURE_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: SubagentFailureEvent) -> SubagentFailureEvent:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        return event

    def record(self, event: SubagentFailureEvent) -> SubagentFailureEvent:
        return self.append(event)

    def update(self, event_id: str, **updates: Any) -> SubagentFailureEvent:
        event = self.get(event_id)
        if event is None:
            raise ValueError(f"Failure event {event_id} not found")
        for key, value in updates.items():
            if not hasattr(event, key):
                raise ValueError(f"Unknown failure event field: {key}")
            setattr(event, key, value)
        return self.append(event)

    def get(self, event_id: str) -> SubagentFailureEvent | None:
        return self.latest_events().get(event_id)

    def latest_events(self) -> dict[str, SubagentFailureEvent]:
        latest: dict[str, SubagentFailureEvent] = {}
        for event in self.read_all():
            latest[event.event_id] = event
        return latest

    def unresolved_events(self, *, limit: int | None = None) -> list[SubagentFailureEvent]:
        events = [
            event
            for event in self.latest_events().values()
            if event.recovery_status not in {"resolved"}
        ]
        events.sort(key=lambda item: item.timestamp, reverse=True)
        return events[:limit] if limit is not None else events

    def read_all(self) -> list[SubagentFailureEvent]:
        if not self.path.exists():
            return []
        events: list[SubagentFailureEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                allowed = set(SubagentFailureEvent.__dataclass_fields__)
                cleaned = {key: value for key, value in raw.items() if key in allowed}
                if "validation_result" not in cleaned:
                    cleaned["validation_result"] = default_validation_result()
                events.append(SubagentFailureEvent(**cleaned))
        return events


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
