#!/usr/bin/env python3
"""Reusable run locks for Rocky assistant helpers."""
from __future__ import annotations

import hashlib
import os
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from assistant_audit_log import AssistantAuditLog
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso


SCHEDULER_POLICY_VERSION = "rocky-scheduler-policy-v1"


@dataclass
class RunLockResult:
    status: str
    acquired: bool
    lock_key: str
    workflow: str
    idempotency_key: str
    reason: str
    row: dict[str, Any] | None = None
    audit_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_lock_key(*, workflow: str, idempotency_key: str) -> str:
    payload = f"{workflow}:{idempotency_key}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"lock:{digest}"


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _audit_lock_event(
    *,
    audit_log: AssistantAuditLog | None,
    event_type: str,
    workflow: str,
    idempotency_key: str,
    decision: str,
    reason: str,
    artifacts: dict[str, Any],
) -> str | None:
    if audit_log is None:
        return None
    event = audit_log.record_event(
        event_type=event_type,
        workflow=workflow,
        idempotency_key=idempotency_key,
        policy_version=SCHEDULER_POLICY_VERSION,
        decision=decision,
        reason=reason,
        artifacts=artifacts,
    )
    return event.audit_id


def acquire_run_lock(
    *,
    workflow: str,
    idempotency_key: str,
    ttl_seconds: int = 900,
    owner: str | None = None,
    db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    write_audit: bool = True,
    metadata: dict[str, Any] | None = None,
) -> RunLockResult:
    lock_key = make_lock_key(workflow=workflow, idempotency_key=idempotency_key)
    state = AssistantSchedulerState(db_path)
    audit_log = AssistantAuditLog(ledger_path) if write_audit else None
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires = (now_dt + timedelta(seconds=max(1, int(ttl_seconds)))).isoformat()
    result, row = state.acquire_lock_record(
        lock_key=lock_key,
        workflow=workflow,
        idempotency_key=idempotency_key,
        owner=owner or _owner(),
        pid=os.getpid(),
        acquired_at=now,
        expires_at=expires,
        metadata=metadata or {},
    )
    if result == "duplicate_blocked":
        audit_id = _audit_lock_event(
            audit_log=audit_log,
            event_type="lock.duplicate_blocked",
            workflow=workflow,
            idempotency_key=idempotency_key,
            decision="blocked",
            reason="active_lock_exists",
            artifacts={"lock_key": lock_key, "lock": row},
        )
        return RunLockResult(
            status=result,
            acquired=False,
            lock_key=lock_key,
            workflow=workflow,
            idempotency_key=idempotency_key,
            reason="active_lock_exists",
            row=row,
            audit_id=audit_id,
        )

    event_type = "lock.stale_recovered" if result == "stale_recovered" else "lock.acquired"
    audit_id = _audit_lock_event(
        audit_log=audit_log,
        event_type=event_type,
        workflow=workflow,
        idempotency_key=idempotency_key,
        decision="allowed",
        reason=result,
        artifacts={"lock_key": lock_key, "lock": row},
    )
    return RunLockResult(
        status=result,
        acquired=True,
        lock_key=lock_key,
        workflow=workflow,
        idempotency_key=idempotency_key,
        reason=result,
        row=row,
        audit_id=audit_id,
    )


def release_run_lock(
    *,
    workflow: str,
    idempotency_key: str,
    db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    write_audit: bool = True,
) -> RunLockResult:
    lock_key = make_lock_key(workflow=workflow, idempotency_key=idempotency_key)
    state = AssistantSchedulerState(db_path)
    row = state.release_lock_record(lock_key=lock_key, released_at=utc_now_iso())
    audit_log = AssistantAuditLog(ledger_path) if write_audit else None
    audit_id = _audit_lock_event(
        audit_log=audit_log,
        event_type="lock.released",
        workflow=workflow,
        idempotency_key=idempotency_key,
        decision="completed",
        reason="lock_released",
        artifacts={"lock_key": lock_key, "lock": row},
    )
    return RunLockResult(
        status="released",
        acquired=False,
        lock_key=lock_key,
        workflow=workflow,
        idempotency_key=idempotency_key,
        reason="lock_released",
        row=row,
        audit_id=audit_id,
    )


def smoke_lock_cycle(
    *,
    workflow: str,
    idempotency_key: str,
    ttl_seconds: int = 60,
    db_path: Path | str | None = None,
    ledger_path: Path | str | None = None,
    write_audit: bool = True,
) -> dict[str, Any]:
    first = acquire_run_lock(
        workflow=workflow,
        idempotency_key=idempotency_key,
        ttl_seconds=ttl_seconds,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"smoke": True},
    )
    second = acquire_run_lock(
        workflow=workflow,
        idempotency_key=idempotency_key,
        ttl_seconds=ttl_seconds,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"smoke": True},
    )
    released = release_run_lock(
        workflow=workflow,
        idempotency_key=idempotency_key,
        db_path=db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
    )
    return {
        "status": "ok" if first.acquired and second.status == "duplicate_blocked" else "failed",
        "first": first.to_dict(),
        "second": second.to_dict(),
        "released": released.to_dict(),
        "side_effects": ["local_scheduler_db", "assistant_audit_log"] if write_audit else ["local_scheduler_db"],
        "helpers_run": False,
        "calendar_write_attempted": False,
        "notifications_sent": False,
    }
