#!/usr/bin/env python3
"""
Durable runner for Bob implementation jobs.

The runner is intentionally small: it launches a caller-provided Bob/Codex
command, captures stdout/stderr, and appends state transitions to a JSONL
ledger. Notification is tracked separately from process completion so Rocky
does not confuse "Bob exited 0" with "Rocky/Dusan were informed".
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bob_job_reporting import report_bob_stage

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = ROOT / "improvement" / "bob_job_ledger.jsonl"
DEFAULT_ARTIFACT_ROOT = ROOT / "improvement" / "bob_jobs"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BobJobRecord:
    job_id: str
    job_name: str
    brief_path: str
    command: list[str]
    timestamp_started: str
    timestamp_finished: str | None = None
    process_status: str = "running"
    exit_code: int | None = None
    rocky_notification_status: str = "not_attempted"
    rocky_notification_attempted_at: str | None = None
    rocky_notification_error: str | None = None
    dusan_notification_status: str = "not_attempted"
    dusan_notification_confirmed_at: str | None = None
    dusan_notification_note: str | None = None
    artifact_dir: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    summary_path: str | None = None
    human_summary: str | None = None


class BobJobLedger:
    def __init__(self, ledger_path: Path | None = None):
        self.path = ledger_path or DEFAULT_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: BobJobRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def latest_events(self) -> dict[str, BobJobRecord]:
        latest: dict[str, BobJobRecord] = {}
        if not self.path.exists():
            return latest
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                latest[data["job_id"]] = BobJobRecord(**data)
        return latest

    def get(self, job_id: str) -> BobJobRecord | None:
        return self.latest_events().get(job_id)

    def recent(self, limit: int = 10) -> list[BobJobRecord]:
        records = list(self.latest_events().values())
        records.sort(key=lambda record: record.timestamp_started, reverse=True)
        return records[:limit]

    def mark_dusan_notified(self, job_id: str, note: str | None = None) -> BobJobRecord:
        record = self.get(job_id)
        if record is None:
            raise ValueError(f"Bob job {job_id} not found")
        record.dusan_notification_status = "confirmed"
        record.dusan_notification_confirmed_at = _utc_now()
        record.dusan_notification_note = note
        record.human_summary = summarize_record(record)
        self.append(record)
        return record


def summarize_record(record: BobJobRecord) -> str:
    process = {
        "running": "still running",
        "succeeded": "exited successfully",
        "failed": f"exited non-zero ({record.exit_code})",
    }.get(record.process_status, record.process_status)
    rocky = record.rocky_notification_status
    dusan = record.dusan_notification_status
    return (
        f"Bob job {record.job_name} is {process}; "
        f"Rocky notification={rocky}; Dusan notification={dusan}."
    )


def _relative_or_absolute(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def _artifact_paths(artifact_root: Path, job_id: str) -> tuple[Path, Path, Path, Path]:
    artifact_dir = artifact_root / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return (
        artifact_dir,
        artifact_dir / "stdout.log",
        artifact_dir / "stderr.log",
        artifact_dir / "summary.json",
    )


def _notification_env(record: BobJobRecord) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BOB_JOB_ID": record.job_id,
            "BOB_JOB_NAME": record.job_name,
            "BOB_JOB_PROCESS_STATUS": record.process_status,
            "BOB_JOB_EXIT_CODE": "" if record.exit_code is None else str(record.exit_code),
            "BOB_JOB_BRIEF_PATH": record.brief_path,
            "BOB_JOB_SUMMARY_PATH": record.summary_path or "",
            "BOB_JOB_STDOUT_PATH": record.stdout_path or "",
            "BOB_JOB_STDERR_PATH": record.stderr_path or "",
        }
    )
    return env


def _attempt_rocky_notification(record: BobJobRecord, notify_command: str | None) -> BobJobRecord:
    if not notify_command:
        record.rocky_notification_status = "not_attempted"
        return record

    record.rocky_notification_attempted_at = _utc_now()
    completed = subprocess.run(
        notify_command,
        shell=True,
        cwd=ROOT,
        env=_notification_env(record),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if completed.returncode == 0:
        record.rocky_notification_status = "confirmed"
        record.rocky_notification_error = None
    else:
        record.rocky_notification_status = "failed"
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        record.rocky_notification_error = detail[:500]
    return record


def run_bob_job(
    *,
    brief_path: Path,
    job_name: str,
    command: list[str],
    ledger_path: Path | None = None,
    artifact_root: Path | None = None,
    notify_command: str | None = None,
    report_discord: bool = False,
    report_dry_run: bool = False,
    report_ledger_path: Path | None = None,
) -> BobJobRecord:
    if not command:
        raise ValueError("A Bob/Codex command must be provided after --")
    if not brief_path.exists():
        raise ValueError(f"Brief not found: {brief_path}")

    ledger = BobJobLedger(ledger_path)
    job_id = f"bob-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    artifact_dir, stdout_path, stderr_path, summary_path = _artifact_paths(
        artifact_root or DEFAULT_ARTIFACT_ROOT,
        job_id,
    )
    record = BobJobRecord(
        job_id=job_id,
        job_name=job_name,
        brief_path=_relative_or_absolute(brief_path),
        command=command,
        timestamp_started=_utc_now(),
        artifact_dir=_relative_or_absolute(artifact_dir),
        stdout_path=_relative_or_absolute(stdout_path),
        stderr_path=_relative_or_absolute(stderr_path),
        summary_path=_relative_or_absolute(summary_path),
    )
    record.human_summary = summarize_record(record)
    ledger.append(record)
    if report_discord or report_dry_run:
        report_bob_stage(
            record,
            "job_started",
            ledger_path=report_ledger_path,
            dry_run=report_dry_run,
        )

    with stdout_path.open("w", encoding="utf-8") as stdout_handle:
        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )

    record.timestamp_finished = _utc_now()
    record.exit_code = completed.returncode
    record.process_status = "succeeded" if completed.returncode == 0 else "failed"
    record.human_summary = summarize_record(record)
    ledger.append(record)

    record = _attempt_rocky_notification(record, notify_command)
    record.human_summary = summarize_record(record)
    summary_path.write_text(json.dumps(asdict(record), indent=2, ensure_ascii=False), encoding="utf-8")
    if report_discord or report_dry_run:
        report_bob_stage(
            record,
            "completed" if record.process_status == "succeeded" else "failed_blocked",
            ledger_path=report_ledger_path,
            dry_run=report_dry_run,
        )
    ledger.append(record)
    return record


def format_bob_jobs(records: list[BobJobRecord]) -> str:
    if not records:
        return "No Bob jobs recorded yet."
    lines = [f"Bob Jobs (latest {len(records)})", "=" * 60]
    for record in records:
        started = record.timestamp_started[:19] if record.timestamp_started else "?"
        lines.append(
            f"[{record.process_status}] {record.job_id} {record.job_name} [{started}] "
            f"rocky={record.rocky_notification_status} dusan={record.dusan_notification_status}"
        )
        if record.human_summary:
            lines.append(f"     {record.human_summary}")
        if record.summary_path:
            lines.append(f"     summary: {record.summary_path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Bob job with durable completion state.")
    parser.add_argument("--brief", required=True, help="Brief path for Bob to implement.")
    parser.add_argument("--job-name", required=True, help="Human-readable Bob job name.")
    parser.add_argument("--ledger-path", help="Override ledger path, mainly for tests.")
    parser.add_argument("--artifact-root", help="Override artifact root, mainly for tests.")
    parser.add_argument(
        "--notify-command",
        help=(
            "Optional local command used as a best-effort Rocky notification callback. "
            "It receives BOB_JOB_* environment variables."
        ),
    )
    parser.add_argument(
        "--report-discord",
        action="store_true",
        help="Post compact Bob job start/terminal updates to the dedicated Bob Discord channel.",
    )
    parser.add_argument(
        "--report-dry-run",
        action="store_true",
        help="Render and record Bob report updates without live Discord delivery.",
    )
    parser.add_argument(
        "--report-ledger-path",
        help="Override Bob report ledger path, mainly for tests.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to launch after --.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    try:
        record = run_bob_job(
            brief_path=Path(args.brief),
            job_name=args.job_name,
            command=command,
            ledger_path=Path(args.ledger_path) if args.ledger_path else None,
            artifact_root=Path(args.artifact_root) if args.artifact_root else None,
            notify_command=args.notify_command,
            report_discord=args.report_discord,
            report_dry_run=args.report_dry_run,
            report_ledger_path=Path(args.report_ledger_path) if args.report_ledger_path else None,
        )
    except (ValueError, subprocess.TimeoutExpired) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(record), indent=2, ensure_ascii=False))
    return 0 if record.process_status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
