#!/usr/bin/env python3
"""
Durable Discord reporting for Bob jobs.

Bob job state remains the source of truth. This module only renders and posts
compact Rocky-owned updates from that state, then records report delivery state
separately so Discord chatter never becomes the primary completion signal.
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_LEDGER_PATH = ROOT / "improvement" / "bob_report_ledger.jsonl"
DEFAULT_BOB_REPORT_CHANNEL_ID = "1485014171459260688"
DEFAULT_DISCORD_GUILD_ID = "1484954575675981906"
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"

BOB_REPORT_STAGES = (
    "job_started",
    "plan_ready",
    "implementation_started",
    "completed",
    "failed_blocked",
    "rocky_reviewed_accepted",
)

STAGE_LABELS = {
    "job_started": "Bob job started",
    "plan_ready": "Bob plan ready",
    "implementation_started": "Bob implementation started",
    "completed": "Bob job completed",
    "failed_blocked": "Bob job failed or blocked",
    "rocky_reviewed_accepted": "Rocky reviewed and accepted",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BobReportRecord:
    report_id: str
    job_id: str
    job_name: str
    stage: str
    channel_id: str
    timestamp: str
    status: str
    message: str
    message_ids: list[str] | None = None
    error: str | None = None
    brief_path: str | None = None
    plan_path: str | None = None
    summary_path: str | None = None
    artifact_paths: list[str] | None = None


class BobReportLedger:
    def __init__(self, ledger_path: Path | None = None):
        self.path = ledger_path or DEFAULT_REPORT_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: BobReportRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def records(self) -> list[BobReportRecord]:
        if not self.path.exists():
            return []
        loaded: list[BobReportRecord] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                loaded.append(
                    BobReportRecord(
                        report_id=data["report_id"],
                        job_id=data["job_id"],
                        job_name=data.get("job_name", ""),
                        stage=data["stage"],
                        channel_id=data.get("channel_id", DEFAULT_BOB_REPORT_CHANNEL_ID),
                        timestamp=data.get("timestamp", ""),
                        status=data.get("status", "unknown"),
                        message=data.get("message", ""),
                        message_ids=data.get("message_ids"),
                        error=data.get("error"),
                        brief_path=data.get("brief_path"),
                        plan_path=data.get("plan_path"),
                        summary_path=data.get("summary_path"),
                        artifact_paths=data.get("artifact_paths"),
                    )
                )
        return loaded

    def recent(self, limit: int = 10) -> list[BobReportRecord]:
        records = self.records()
        records.sort(key=lambda record: record.timestamp, reverse=True)
        return records[:limit]

    def latest_for_stage(self, job_id: str, stage: str) -> BobReportRecord | None:
        candidates = [
            record for record in self.records() if record.job_id == job_id and record.stage == stage
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda record: record.timestamp, reverse=True)
        return candidates[0]

    def was_posted(self, job_id: str, stage: str) -> bool:
        return any(
            record.job_id == job_id and record.stage == stage and record.status == "posted"
            for record in self.records()
        )


def _relative_or_absolute(path: Path | str | None) -> str | None:
    if not path:
        return None
    resolved = Path(path)
    if not resolved.is_absolute():
        return str(resolved)
    try:
        return str(resolved.resolve().relative_to(ROOT))
    except ValueError:
        return str(resolved.resolve())


def _compact(value: str | None, *, max_len: int = 500) -> str | None:
    if not value:
        return None
    stripped = " ".join(value.strip().split())
    if len(stripped) <= max_len:
        return stripped
    return stripped[: max_len - 3].rstrip() + "..."


def derive_stage_from_job(record: Any) -> str:
    if record.process_status == "failed":
        return "failed_blocked"
    if record.process_status == "succeeded":
        return "completed"
    return "job_started"


def render_bob_report_message(
    record: Any,
    stage: str,
    *,
    summary: str | None = None,
    plan_path: str | None = None,
    artifact_paths: list[str] | None = None,
) -> str:
    if stage not in BOB_REPORT_STAGES:
        raise ValueError(f"Unsupported Bob report stage: {stage}")

    lines = [
        f"**{STAGE_LABELS[stage]}**",
        f"- Job: `{record.job_name}`",
        f"- Job id: `{record.job_id}`",
        f"- Process status: `{record.process_status}`",
    ]
    if getattr(record, "exit_code", None) is not None:
        lines.append(f"- Exit code: `{record.exit_code}`")
    lines.append(f"- Brief: `{record.brief_path}`")

    plan_ref = _relative_or_absolute(plan_path)
    if plan_ref:
        lines.append(f"- Plan: `{plan_ref}`")
    if getattr(record, "summary_path", None):
        lines.append(f"- Job summary: `{record.summary_path}`")

    compact_summary = _compact(summary)
    if not compact_summary and getattr(record, "human_summary", None):
        compact_summary = _compact(record.human_summary)
    if compact_summary:
        lines.append(f"- Summary: {compact_summary}")

    for artifact in artifact_paths or []:
        artifact_ref = _relative_or_absolute(artifact)
        if artifact_ref:
            lines.append(f"- Artifact: `{artifact_ref}`")

    if stage == "completed":
        lines.append("- Review state: Rocky review is not implied by process success.")
    elif stage == "plan_ready":
        lines.append("- Review state: plan ready does not mean Rocky accepted it.")
    elif stage == "rocky_reviewed_accepted":
        lines.append("- Review state: Rocky accepted this stage; Dusan notification is tracked separately.")
    elif stage == "failed_blocked":
        lines.append("- Attention: review stderr/summary artifacts before treating this as resolved.")

    return "\n".join(lines)


def _split_discord_message(content: str, max_length: int = 1900) -> list[str]:
    if len(content) <= max_length:
        return [content]
    messages: list[str] = []
    current = ""
    for line in content.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) > max_length and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _load_discord_token(config_path: Path = OPENCLAW_CONFIG_PATH) -> str:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    token = (((payload.get("channels") or {}).get("discord") or {}).get("token") or "").strip()
    if not token:
        raise RuntimeError("Discord bot token is not configured in openclaw.json")
    return token


def post_bob_report_to_discord(message: str, *, channel_id: str = DEFAULT_BOB_REPORT_CHANNEL_ID) -> dict[str, Any]:
    token = _load_discord_token()
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    message_ids: list[str] = []
    for chunk in _split_discord_message(message):
        payload = json.dumps(
            {
                "content": chunk,
                "allowed_mentions": {"parse": []},
            }
        ).encode("utf-8")
        req = request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "OpenClaw-BobReporting/1.0",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
                message_ids.append(body.get("id"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord API error {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Discord delivery failed: {exc.reason}") from exc
    return {
        "status": "posted",
        "channel_id": channel_id,
        "message_ids": message_ids,
        "message_count": len(message_ids),
    }


def report_bob_stage(
    record: Any,
    stage: str,
    *,
    summary: str | None = None,
    plan_path: str | None = None,
    artifact_paths: list[str] | None = None,
    channel_id: str = DEFAULT_BOB_REPORT_CHANNEL_ID,
    ledger_path: Path | None = None,
    post_func: Callable[[str, str], dict[str, Any]] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> BobReportRecord:
    ledger = BobReportLedger(ledger_path)
    message = render_bob_report_message(
        record,
        stage,
        summary=summary,
        plan_path=plan_path,
        artifact_paths=artifact_paths,
    )

    if not force and ledger.was_posted(record.job_id, stage):
        latest = ledger.latest_for_stage(record.job_id, stage)
        skipped = BobReportRecord(
            report_id=f"bobreport-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
            job_id=record.job_id,
            job_name=record.job_name,
            stage=stage,
            channel_id=channel_id,
            timestamp=_utc_now(),
            status="skipped_duplicate",
            message=message,
            error=f"Stage already posted at {latest.timestamp if latest else 'unknown time'}",
            brief_path=getattr(record, "brief_path", None),
            plan_path=_relative_or_absolute(plan_path),
            summary_path=getattr(record, "summary_path", None),
            artifact_paths=[item for item in (_relative_or_absolute(path) for path in artifact_paths or []) if item],
        )
        ledger.append(skipped)
        return skipped

    report = BobReportRecord(
        report_id=f"bobreport-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        job_id=record.job_id,
        job_name=record.job_name,
        stage=stage,
        channel_id=channel_id,
        timestamp=_utc_now(),
        status="dry_run" if dry_run else "posting",
        message=message,
        brief_path=getattr(record, "brief_path", None),
        plan_path=_relative_or_absolute(plan_path),
        summary_path=getattr(record, "summary_path", None),
        artifact_paths=[item for item in (_relative_or_absolute(path) for path in artifact_paths or []) if item],
    )
    if dry_run:
        ledger.append(report)
        return report

    poster = post_func or (lambda msg, chan: post_bob_report_to_discord(msg, channel_id=chan))
    try:
        result = poster(message, channel_id)
    except Exception as exc:  # noqa: BLE001 - delivery failures must be recorded, not hidden
        report.status = "failed"
        report.error = str(exc)[:500]
        ledger.append(report)
        return report

    report.status = "posted" if result.get("status") == "posted" else str(result.get("status") or "unknown")
    report.message_ids = result.get("message_ids")
    if report.status != "posted":
        report.error = json.dumps(result, ensure_ascii=False)[:500]
    ledger.append(report)
    return report


def format_bob_reports(records: list[BobReportRecord]) -> str:
    if not records:
        return "No Bob report records yet."
    lines = [f"Bob Reports (latest {len(records)})", "=" * 60]
    for record in records:
        ts = record.timestamp[:19] if record.timestamp else "?"
        lines.append(
            f"[{record.status}] {record.job_id} {record.stage} "
            f"channel={record.channel_id} [{ts}]"
        )
        if record.error:
            lines.append(f"     {record.error}")
        if record.summary_path:
            lines.append(f"     summary: {record.summary_path}")
        if record.plan_path:
            lines.append(f"     plan: {record.plan_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from argparse import ArgumentParser
    from bob_job_runner import BobJobLedger

    parser = ArgumentParser(description="Render/report Bob durable job state to Discord.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--stage", choices=BOB_REPORT_STAGES)
    parser.add_argument("--summary")
    parser.add_argument("--plan-path")
    parser.add_argument("--artifact-path", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    job = BobJobLedger().get(args.job_id)
    if job is None:
        print(f"[ERROR] Bob job {args.job_id} not found", file=sys.stderr)
        return 1
    report = report_bob_stage(
        job,
        args.stage or derive_stage_from_job(job),
        summary=args.summary,
        plan_path=args.plan_path,
        artifact_paths=args.artifact_path,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    return 0 if report.status in {"posted", "dry_run", "skipped_duplicate"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
