#!/usr/bin/env python3
"""Structured assistant action audit log for Rocky.

This ledger is for decisions, proposals, dry-runs, and side effects. It is
separate from rocky_run_ledger.py, which tracks workflow execution state.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT_LEDGER_PATH = ROOT / "improvement" / "assistant_audit.jsonl"

VALID_EVENT_TYPES = {
    "calendar.write_requested",
    "calendar.event_created",
    "calendar.event_deleted",
    "calendar.write_failed",
    "calendar.delete_blocked",
    "calendar.delete_failed",
    "calendar.state_marked_stale",
    "calendar.state_reconciled",
    "calendar.write_health_checked",
    "calendar.proposal_created",
    "calendar.proposal_blocked",
    "calendar.conflict_detected",
    "policy.violation",
    "policy.allowed",
    "dry_run.completed",
    "scheduler.health_ok",
    "scheduler.health_degraded",
    "scheduler.health_blocked",
    "scheduler.run_expected",
    "scheduler.run_observed",
    "scheduler.run_missed",
    "scheduler.run_stale",
    "scheduler.run_failed",
    "scheduler.dead_letter_created",
    "scheduler.dead_letter_resolved",
    "scheduler.recovery_needed",
    "scheduler.cron_disabled_verified",
    "scheduler.cron_unexpected_enabled",
    "assistant.notification_sent",
    "assistant.notification_failed",
    "assistant.outcome_observed",
    "assistant.preference_model_updated",
    "assistant.learning_proposal_created",
    "assistant.learning_proposal_applied",
    "assistant.learning_degraded",
    "training_calendar.reconciled",
    "training_calendar.fix_applied",
    "task.candidate_detected",
    "task.created",
    "task.updated",
    "task.duplicate_detected",
    "task.reminder_sent",
    "task.reminder_skipped",
    "task.calendar_proposed",
    "task.calendar_booked",
    "task.detection_failed",
    "task.identity_resolved",
    "task.identity_migrated",
    "task.lifecycle_updated",
    "task.reminder_updated",
    "task.source_signal_collected",
    "task.command_interpreted",
    "task.command_applied",
    "task.command_blocked",
    "task.capture_failed",
    "lock.acquired",
    "lock.released",
    "lock.duplicate_blocked",
    "lock.stale_detected",
    "lock.stale_recovered",
}
VALID_DECISIONS = {
    "allowed",
    "blocked",
    "created",
    "completed",
    "failed",
    "degraded",
    "observed",
    "missed",
    "recovered",
}
SENSITIVE_KEY_PARTS = {
    "auth",
    "body",
    "content",
    "cookie",
    "credential",
    "email_body",
    "html",
    "password",
    "raw",
    "secret",
    "token",
    "transcript",
}
MAX_SAFE_STRING_CHARS = 500
SENSITIVE_STRING_RE = re.compile(
    r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth)",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _redacted_value(value: Any) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return {
        "redacted": True,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "chars": len(text),
    }


def redact_payload(value: Any, *, parent_key: str = "") -> Any:
    """Return a log-safe payload with sensitive values removed.

    The audit log should preserve enough evidence to debug decisions without
    storing raw email bodies, transcript bodies, cookies, tokens, or secrets.
    """
    if parent_key and _is_sensitive_key(parent_key):
        return _redacted_value(value)
    if isinstance(value, dict):
        return {
            str(key): redact_payload(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and SENSITIVE_STRING_RE.search(value):
        return _redacted_value(value)
    if isinstance(value, str) and len(value) > MAX_SAFE_STRING_CHARS:
        return {
            "truncated": True,
            "prefix": value[:MAX_SAFE_STRING_CHARS],
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
            "chars": len(value),
        }
    return value


@dataclass
class AssistantAuditEvent:
    audit_id: str
    event_type: str
    actor: str
    workflow: str
    created_at: str
    idempotency_key: str
    policy_version: str
    privacy_class: str
    decision: str
    reason: str
    sources: list[Any] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)


class AssistantAuditLog:
    """Append-only JSONL audit ledger for assistant actions."""

    def __init__(self, ledger_path: Path | str | None = None):
        self.path = Path(ledger_path) if ledger_path else DEFAULT_AUDIT_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_event(
        self,
        *,
        event_type: str,
        actor: str = "rocky",
        workflow: str,
        idempotency_key: str,
        policy_version: str,
        privacy_class: str = "internal",
        decision: str,
        reason: str,
        sources: list[Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> AssistantAuditEvent:
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(f"Unsupported assistant audit event type: {event_type}")
        if decision not in VALID_DECISIONS:
            raise ValueError(f"Unsupported assistant audit decision: {decision}")
        if not workflow:
            raise ValueError("workflow is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        event = AssistantAuditEvent(
            audit_id=str(uuid.uuid4())[:12],
            event_type=event_type,
            actor=actor,
            workflow=workflow,
            created_at=utc_now_iso(),
            idempotency_key=idempotency_key,
            policy_version=policy_version,
            privacy_class=privacy_class,
            decision=decision,
            reason=reason,
            sources=redact_payload(list(sources or [])),
            artifacts=redact_payload(dict(artifacts or {})),
        )
        self._append(event)
        return event

    def recent(self, limit: int = 20) -> list[AssistantAuditEvent]:
        events = self.read_all()
        return events[-max(0, limit):][::-1]

    def read_all(self) -> list[AssistantAuditEvent]:
        if not self.path.exists():
            return []
        events: list[AssistantAuditEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                events.append(AssistantAuditEvent(**json.loads(line)))
        return events

    def _append(self, event: AssistantAuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
