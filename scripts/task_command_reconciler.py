#!/usr/bin/env python3
"""Reconcile task command sources against Rocky's command ledger."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from discord_task_command_reader import read_discord_task_commands
from email_task_command_reader import read_email_task_commands
from meeting_task_signal_reader import collect_meeting_task_signals
from task_command_ledger import TaskCommandLedger, command_fingerprint


def collect_source_commands(
    *,
    sources: list[str] | None = None,
    since_minutes: int = 60,
    since_days: int = 14,
    limit: int = 50,
    discord_payload: dict[str, Any] | None = None,
    email_payload: dict[str, Any] | None = None,
    meeting_payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = sources or ["discord", "email", "meeting"]
    commands: list[dict[str, Any]] = []
    source_payloads: dict[str, Any] = {}
    if "discord" in selected:
        payload = discord_payload if discord_payload is not None else read_discord_task_commands(since_minutes=since_minutes, limit=limit, write_state=False)
        source_payloads["discord"] = _source_summary(payload)
        commands.extend(payload.get("commands") or [])
    if "email" in selected:
        payload = email_payload if email_payload is not None else read_email_task_commands(since_minutes=since_minutes, limit=limit)
        source_payloads["email"] = _source_summary(payload)
        commands.extend(payload.get("commands") or [])
    if "meeting" in selected:
        payload = meeting_payload if meeting_payload is not None else collect_meeting_task_signals(since_days=since_days, limit=limit)
        source_payloads["meeting"] = _source_summary(payload, count_key="signal_count")
        for signal in payload.get("signals") or []:
            commands.append(_meeting_signal_to_command(signal))
    return commands[: max(1, int(limit))], source_payloads


def reconcile_task_commands(
    *,
    sources: list[str] | None = None,
    ledger_db_path: str | Path | None = None,
    since_minutes: int = 60,
    since_days: int = 14,
    limit: int = 50,
    mark_missing: bool = False,
    discord_payload: dict[str, Any] | None = None,
    email_payload: dict[str, Any] | None = None,
    meeting_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ledger = TaskCommandLedger(ledger_db_path)
    commands, source_payloads = collect_source_commands(
        sources=sources,
        since_minutes=since_minutes,
        since_days=since_days,
        limit=limit,
        discord_payload=discord_payload,
        email_payload=email_payload,
        meeting_payload=meeting_payload,
    )
    results: list[dict[str, Any]] = []
    visible_keys: set[tuple[str, str]] = set()
    for command in commands:
        fingerprint = str(command.get("command_fingerprint") or command_fingerprint(command))
        source_ref = str(command.get("source_ref") or "")
        row = ledger.get(source_ref=source_ref, command_fingerprint=fingerprint)
        visible_keys.add((source_ref, fingerprint))
        if row:
            status = _classify_row(row)
            results.append({"source_ref": source_ref, "command_fingerprint": fingerprint, "source_channel": command.get("source_channel"), "status": status, "ledger_status": row.get("status"), "task_page_id": row.get("task_page_id"), "audit_id": row.get("audit_id")})
        else:
            if mark_missing:
                row = ledger.record_seen(command, status="seen", reason="reconcile_mark_missing")
            results.append({"source_ref": source_ref, "command_fingerprint": fingerprint, "source_channel": command.get("source_channel"), "status": "new_unprocessed", "ledger_status": (row or {}).get("status") if mark_missing else None})
    selected_channels = {"meeting" if source == "meeting" else "email" if source == "email" else "discord" for source in (sources or ["discord", "email", "meeting"])}
    for row in ledger.recent(limit=limit * 3):
        key = (str(row.get("source_ref") or ""), str(row.get("command_fingerprint") or ""))
        if str(row.get("source_channel") or "") in selected_channels and key not in visible_keys:
            results.append({
                "source_ref": row.get("source_ref"),
                "command_fingerprint": row.get("command_fingerprint"),
                "source_channel": row.get("source_channel"),
                "status": "source_no_longer_visible",
                "ledger_status": row.get("status"),
                "task_page_id": row.get("task_page_id"),
                "audit_id": row.get("audit_id"),
            })

    counts: dict[str, int] = {}
    for result in results:
        counts[str(result.get("status"))] = counts.get(str(result.get("status")), 0) + 1
    return {
        "status": "ok",
        "reason": "task_command_reconcile_completed",
        "sources": sources or ["discord", "email", "meeting"],
        "source_payloads": source_payloads,
        "results": results,
        "counts": counts,
        "mark_missing": bool(mark_missing),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def recent_task_commands(*, ledger_db_path: str | Path | None = None, limit: int = 20) -> dict[str, Any]:
    ledger = TaskCommandLedger(ledger_db_path)
    rows = ledger.recent(limit=limit)
    return {
        "status": "ok",
        "commands": rows,
        "command_count": len(rows),
        "counts_by_status": ledger.counts_by_status(),
        "counts_by_source": ledger.counts_by_source(),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _meeting_signal_to_command(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "Meeting",
        "source_channel": "meeting",
        "source_ref": signal.get("source_ref"),
        "text": signal.get("summary") or "",
        "created_at": signal.get("observed_at"),
        "meeting_title": signal.get("meeting_title"),
        "owner_hint": signal.get("owner_hint"),
    }


def _source_summary(payload: dict[str, Any], *, count_key: str = "command_count") -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "count": payload.get(count_key, 0),
        "warning_count": payload.get("warning_count", 0),
    }


def _classify_row(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "seen")
    if status in {"applied", "ack_sent"}:
        return "applied"
    if status == "skipped_duplicate":
        return "skipped_duplicate"
    if status in {"blocked", "ack_failed"}:
        return "blocked"
    if status == "manual_review_required":
        return "manual_review_required"
    return "new_unprocessed"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Reconcile task command sources against Rocky's command ledger.")
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--ledger-db", dest="ledger_db")
    parser.add_argument("--since-minutes", type=int, default=60, dest="since_minutes")
    parser.add_argument("--since-days", type=int, default=14, dest="since_days")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--mark-missing", action="store_true", dest="mark_missing")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = reconcile_task_commands(sources=args.sources, ledger_db_path=args.ledger_db, since_minutes=args.since_minutes, since_days=args.since_days, limit=args.limit, mark_missing=args.mark_missing)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task command reconcile: {payload['status']} ({payload['reason']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
