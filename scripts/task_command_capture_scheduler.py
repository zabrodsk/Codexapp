#!/usr/bin/env python3
"""Near-real-time trusted task command capture scheduler for Rocky."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_notification_dispatcher import dispatch_failure_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from discord_task_command_reader import read_discord_task_commands
from email_task_command_reader import read_email_task_commands
from meeting_task_signal_reader import collect_meeting_task_signals
from task_command_acknowledger import maybe_acknowledge_discord_command
from task_command_interpreter import apply_task_command
from task_command_ledger import DEFAULT_LEDGER_DB, TaskCommandLedger, command_fingerprint
from task_command_reconciler import _meeting_signal_to_command


WORKFLOW = "task_command_capture_scheduler"
JOB_NAME = "task_command_capture"
POLICY_VERSION = "rocky-task-command-capture-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/task_command_capture_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 240
UNSAFE_TEXT_RE = re.compile(r"(https?://|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_task_command_capture_scheduler(
    *,
    sources: list[str] | None = None,
    live: bool = False,
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    since_minutes: int = 10,
    limit: int = 20,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    command_ledger_db_path: str | Path | None = DEFAULT_LEDGER_DB,
    discord_payload: dict[str, Any] | None = None,
    email_payload: dict[str, Any] | None = None,
    notion_client: Any | None = None,
    ack_post_func: Any | None = None,
) -> dict[str, Any]:
    selected = sources or ["discord", "email"]
    now = datetime.now(ZoneInfo(TIMEZONE))
    bucket_minute = (now.minute // 5) * 5
    run_key = f"task-command-capture:{now.strftime('%Y-%m-%dT%H')}:{bucket_minute:02d}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=lock_ttl_seconds,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"job_name": JOB_NAME, "live": bool(live), "sources": selected},
    )
    if not lock.acquired:
        return {
            "status": "skipped_duplicate_run",
            "reason": lock.reason,
            "workflow": WORKFLOW,
            "run_idempotency_key": run_key,
            "lock": lock.to_dict(),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    try:
        payload = _run_inner(
            selected=selected,
            live=live,
            notify_failures=notify_failures,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
            since_minutes=since_minutes,
            limit=limit,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            write_audit=write_audit,
            discord_payload=discord_payload,
            email_payload=email_payload,
            notion_client=notion_client,
            command_ledger_db_path=command_ledger_db_path,
            ack_post_func=ack_post_func,
            run_key=run_key,
        )
        return payload
    except Exception as exc:
        payload = {
            "status": "failed",
            "reason": "task_command_capture_exception",
            "workflow": WORKFLOW,
            "job_name": JOB_NAME,
            "run_idempotency_key": run_key,
            "error_class": exc.__class__.__name__,
            "error_hash": _hash_text(str(exc)),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True, failure_class="task_command_capture_exception", notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _run_inner(
    *,
    selected: list[str],
    live: bool,
    notify_failures: bool,
    notification_dry_run: bool,
    notification_channel_id: str | None,
    since_minutes: int,
    limit: int,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    state_file: str | Path | None,
    write_audit: bool,
    discord_payload: dict[str, Any] | None,
    email_payload: dict[str, Any] | None,
    notion_client: Any | None,
    command_ledger_db_path: str | Path | None,
    ack_post_func: Any | None,
    run_key: str,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    ledger = TaskCommandLedger(command_ledger_db_path)
    collection_errors: list[dict[str, Any]] = []
    source_payloads: dict[str, Any] = {}
    if "discord" in selected:
        payload = discord_payload if discord_payload is not None else read_discord_task_commands(since_minutes=since_minutes, limit=limit, write_state=True)
        source_payloads["discord"] = _safe_source_summary(payload)
        if payload.get("status") in {"ok", "degraded"}:
            commands.extend(payload.get("commands") or [])
        else:
            collection_errors.append({"source": "discord", "reason": payload.get("reason") or payload.get("status")})
    if "email" in selected:
        payload = email_payload if email_payload is not None else read_email_task_commands(since_minutes=since_minutes, limit=limit)
        source_payloads["email"] = _safe_source_summary(payload)
        if payload.get("status") in {"ok", "degraded"}:
            commands.extend(payload.get("commands") or [])
        else:
            collection_errors.append({"source": "email", "reason": payload.get("reason") or payload.get("status")})

    if "meeting" in selected:
        payload = collect_meeting_task_signals(limit=limit)
        source_payloads["meeting"] = _safe_source_summary(payload, count_key="signal_count")
        if payload.get("status") in {"ok", "degraded"}:
            commands.extend(_meeting_signal_to_command(signal) for signal in (payload.get("signals") or []))
        else:
            collection_errors.append({"source": "meeting", "reason": payload.get("reason") or payload.get("status")})

    results: list[dict[str, Any]] = []
    tasks_created = 0
    tasks_updated = 0
    manual_review = 0
    ack_sent = 0
    ack_failed = 0
    for command in commands[: max(1, int(limit))]:
        fingerprint = command_fingerprint(command)
        command["command_fingerprint"] = fingerprint
        ledger.record_seen(command, status="seen", reason="task_command_capture_seen")
        result = apply_task_command(
            str(command.get("text") or ""),
            source=str(command.get("source") or "Command"),
            source_ref=str(command.get("source_ref") or f"command:{_hash_text(json.dumps(command, sort_keys=True, default=str))}"),
            live=live,
            notion_client=notion_client,
            ledger_path=ledger_path,
            write_audit=write_audit,
        )
        ledger_status = _ledger_status_for_result(result)
        ledger.update_outcome(
            source_ref=str(command.get("source_ref") or ""),
            command_fingerprint=fingerprint,
            status=ledger_status,
            reason=str(result.get("reason") or result.get("status") or ""),
            task=result.get("task") or {},
            audit_id=result.get("audit_id"),
        )
        ack = {"status": "skipped", "reason": "ack_not_attempted"}
        if live and ledger_status in {"applied", "skipped_duplicate"}:
            ack = maybe_acknowledge_discord_command(command, result, ledger=ledger, post_func=ack_post_func)
            if ack.get("status") == "ack_sent":
                ack_sent += 1
            elif ack.get("status") == "ack_failed":
                ack_failed += 1
        results.append(_safe_command_result(result, ack=ack, command=command))
        if result.get("status") == "created":
            tasks_created += 1
        elif result.get("status") == "updated":
            tasks_updated += 1
        elif result.get("status") in {"manual_review_required", "blocked", "failed"}:
            manual_review += 1

    status = "ok"
    reason = "task_command_capture_completed"
    dead_letter = False
    failure_class = None
    if ack_failed:
        status = "degraded"
        reason = "task_command_ack_failed"
        dead_letter = True
        failure_class = "task_command_ack_failed"
    if collection_errors:
        status = "degraded"
        reason = "task_command_capture_source_degraded"
        dead_letter = True
        failure_class = "task_command_source_degraded"
    if manual_review:
        status = "manual_review_required"
        reason = "task_command_manual_review_required"
        dead_letter = True
        failure_class = "task_command_manual_review_required"

    payload = {
        "status": status,
        "reason": reason,
        "workflow": WORKFLOW,
        "job_name": JOB_NAME,
        "mode": "live" if live else "dry_run",
        "run_idempotency_key": run_key,
        "sources": selected,
        "source_payloads": source_payloads,
        "commands_seen": len(commands),
        "commands_processed": len(results),
        "tasks_created": tasks_created,
        "tasks_updated": tasks_updated,
        "manual_review_count": manual_review,
        "ack_sent_count": ack_sent,
        "ack_failed_count": ack_failed,
        "ledger_counts_by_status": ledger.counts_by_status(),
        "ledger_counts_by_source": ledger.counts_by_source(),
        "results": results,
        "collection_errors": collection_errors,
        "calendar_write_attempted": False,
        "notion_write_attempted": any(item.get("notion_write_attempted") for item in results),
        "checked_at": utc_now_iso(),
    }
    return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=dead_letter, failure_class=failure_class, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)


def _finish(
    payload: dict[str, Any],
    *,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    state_file: str | Path | None,
    write_audit: bool,
    dead_letter: bool,
    failure_class: str | None,
    notify_failures: bool,
    notification_dry_run: bool,
    notification_channel_id: str | None,
) -> dict[str, Any]:
    safe = _redact_payload(payload)
    state = AssistantSchedulerState(scheduler_db_path)
    state.record_job_run(
        job_name=JOB_NAME,
        job_label="Rocky task command capture",
        scheduled_for=str(safe.get("checked_at") or ""),
        status="dead_lettered" if dead_letter else "succeeded",
        idempotency_key=str(safe.get("run_idempotency_key") or ""),
        failure_class=failure_class,
        summary=str(safe.get("reason") or safe.get("status")),
        error_hash=safe.get("error_hash"),
    )
    if dead_letter:
        safe["dead_letter"] = state.upsert_dead_letter(
            job_name=JOB_NAME,
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or ""),
            failure_class=failure_class or "task_command_capture_attention_needed",
            safe_summary=str(safe.get("reason") or "task command capture attention needed"),
            source_refs=["discord:task-command-capture", "agentmail:task-command-capture"],
            recovery_hint="Inspect task-command-capture output, source command refs, and Notion task matches before rerunning.",
            error_hash=safe.get("error_hash"),
        )
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="task.source_signal_collected",
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or ""),
            policy_version=POLICY_VERSION,
            decision="blocked" if dead_letter else "observed",
            reason=str(safe.get("reason") or safe.get("status")),
            sources=["discord:task-command-capture", "agentmail:task-command-capture"],
            artifacts={
                "status": safe.get("status"),
                "commands_seen": safe.get("commands_seen"),
                "commands_processed": safe.get("commands_processed"),
                "manual_review_count": safe.get("manual_review_count"),
                "ack_sent_count": safe.get("ack_sent_count"),
                "ack_failed_count": safe.get("ack_failed_count"),
            },
        )
        safe["audit_id"] = event.audit_id
    if state_file:
        _write_state_file(Path(state_file), safe)
    if notify_failures and dead_letter:
        safe["notification"] = dispatch_failure_notification(
            safe,
            channel_id=notification_channel_id or "1485710572325703901",
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            dry_run=notification_dry_run,
        )
    return safe


def _safe_source_summary(payload: dict[str, Any], *, count_key: str = "command_count") -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "command_count": payload.get(count_key, 0),
        "warning_count": payload.get("warning_count", 0),
    }


def _ledger_status_for_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    if status in {"created", "updated"}:
        return "applied"
    if status == "skipped" or "duplicate" in reason:
        return "skipped_duplicate"
    if status == "manual_review_required":
        return "manual_review_required"
    if status in {"blocked", "failed"}:
        return "blocked"
    return "seen"


def _safe_command_result(result: dict[str, Any], *, ack: dict[str, Any] | None = None, command: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "idempotency_key": result.get("idempotency_key") or result.get("dedupe_key"),
        "audit_id": result.get("audit_id"),
        "notion_write_attempted": bool(result.get("notion_write_attempted")),
        "task": result.get("task"),
        "match": result.get("match"),
        "source_ref": (command or {}).get("source_ref"),
        "source_channel": (command or {}).get("source_channel"),
        "command_fingerprint": (command or {}).get("command_fingerprint"),
        "ack": ack or {"status": "skipped", "reason": "ack_not_attempted"},
    }


def _write_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run_at": utc_now_iso(),
        "last_status": payload.get("status"),
        "reason": payload.get("reason"),
        "run_idempotency_key": payload.get("run_idempotency_key"),
        "commands_seen": payload.get("commands_seen", 0),
        "commands_processed": payload.get("commands_processed", 0),
        "tasks_created": payload.get("tasks_created", 0),
        "tasks_updated": payload.get("tasks_updated", 0),
        "manual_review_count": payload.get("manual_review_count", 0),
        "ack_sent_count": payload.get("ack_sent_count", 0),
        "ack_failed_count": payload.get("ack_failed_count", 0),
        "ledger_counts_by_status": payload.get("ledger_counts_by_status", {}),
        "ledger_counts_by_source": payload.get("ledger_counts_by_source", {}),
        "error_hash": payload.get("error_hash"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items() if str(key).lower() not in {"body", "content", "raw", "transcript"}}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": _hash_text(value), "chars": len(value)}
    return value


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky task command capture scheduler.")
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--since-minutes", type=int, default=10, dest="since_minutes")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--lock-ttl-seconds", type=int, default=DEFAULT_LOCK_TTL_SECONDS, dest="lock_ttl_seconds")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--command-ledger-db", default=str(DEFAULT_LEDGER_DB), dest="command_ledger_db")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_task_command_capture_scheduler(
        sources=args.sources,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        since_minutes=args.since_minutes,
        limit=args.limit,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
        command_ledger_db_path=args.command_ledger_db,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task command capture: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
