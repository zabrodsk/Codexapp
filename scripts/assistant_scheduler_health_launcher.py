#!/usr/bin/env python3
"""LaunchAgent-safe entrypoint for Rocky assistant scheduler health checks.

This runner is intentionally read-only with respect to user-facing systems:
it does not run Betty, send Discord alerts by default, or write calendar events.
Its expected side effects are local scheduler state, locks, and assistant audit
events.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_health import evaluate_all_scheduler_jobs, format_scheduler_health_report


WORKFLOW = "assistant_scheduler_health"
DEFAULT_LOCK_TTL_SECONDS = 600


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rocky assistant scheduler health LaunchAgent runner.")
    parser.add_argument("--job", help="Optional scheduler job name to check.")
    parser.add_argument("--state-db", dest="state_db", help="Optional assistant scheduler SQLite path.")
    parser.add_argument("--audit-ledger", dest="audit_ledger", help="Optional assistant audit JSONL path.")
    parser.add_argument(
        "--lock-ttl-seconds",
        type=int,
        default=DEFAULT_LOCK_TTL_SECONDS,
        dest="lock_ttl_seconds",
        help="Duplicate-run lock TTL in seconds.",
    )
    parser.add_argument(
        "--alert-mode",
        choices=["none", "discord-on-blocked"],
        default="none",
        dest="alert_mode",
        help="Alert mode. Sprint 2.1 defaults to no outbound notifications.",
    )
    parser.add_argument("--channel-id", dest="channel_id", help="Reserved Discord channel id for future alert mode.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable report.",
    )
    return parser


def _lock_idempotency_key(job: str | None, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    minute_key = now.strftime("%Y%m%dT%H%M")
    job_key = job or "all"
    return f"scheduler-health:{job_key}:{minute_key}"


def _with_launcher_metadata(payload: dict[str, Any], *, args: argparse.Namespace) -> dict[str, Any]:
    payload = dict(payload)
    payload["launcher"] = {
        "workflow": WORKFLOW,
        "mode": "launchagent",
        "alert_mode": args.alert_mode,
        "notifications_sent": False,
        "calendar_write_attempted": False,
        "helpers_run": False,
    }
    if args.alert_mode != "none":
        payload["launcher"]["alert_skipped"] = "discord_alerts_not_enabled_by_default_in_sprint_2_1"
    payload["notifications_sent"] = False
    payload["calendar_write_attempted"] = False
    payload["helpers_run"] = False
    return payload


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    idempotency_key = _lock_idempotency_key(args.job)
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        ttl_seconds=args.lock_ttl_seconds,
        db_path=args.state_db,
        ledger_path=args.audit_ledger,
        write_audit=True,
        metadata={
            "job": args.job or "all",
            "launcher": "assistant_scheduler_health_launcher",
            "alert_mode": args.alert_mode,
        },
    )
    if not lock.acquired:
        payload = {
            "status": "skipped_duplicate",
            "reason": lock.reason,
            "idempotency_key": idempotency_key,
            "lock": lock.to_dict(),
            "jobs": [],
            "helpers_run": False,
            "notifications_sent": False,
            "calendar_write_attempted": False,
        }
        return 0, _with_launcher_metadata(payload, args=args)

    try:
        payload = evaluate_all_scheduler_jobs(
            job_name=args.job,
            state_db_path=args.state_db,
            audit_log_path=args.audit_ledger,
            write_state=True,
            write_audit=True,
        )
        payload["idempotency_key"] = idempotency_key
        payload["lock"] = lock.to_dict()
        exit_code = 1 if payload.get("status") == "blocked" else 0
        return exit_code, _with_launcher_metadata(payload, args=args)
    finally:
        release_run_lock(
            workflow=WORKFLOW,
            idempotency_key=idempotency_key,
            db_path=args.state_db,
            ledger_path=args.audit_ledger,
            write_audit=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code, payload = run(args)
    except Exception as exc:
        payload = {
            "status": "failed",
            "failure_class": "launcher_runtime_error",
            "summary": str(exc),
            "helpers_run": False,
            "notifications_sent": False,
            "calendar_write_attempted": False,
        }
        if args.json_output:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Assistant scheduler health launcher failed: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_scheduler_health_report(payload))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
