#!/usr/bin/env python3
"""Capture Dusan's Discord context notes for meeting prep and day planning."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request

from discord_task_command_reader import DEFAULT_OPENCLAW_CONFIG_PATH, DISCORD_API_BASE, _load_config
from meeting_context_note_ledger import (
    DEFAULT_LEDGER_DB,
    MeetingContextNoteLedger,
    hash_text,
    note_fingerprint,
    safe_text_value,
)

DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/meeting_context_note_capture.json")
CONTEXT_NOTE_RE = re.compile(
    r"^\s*(?:<@!?\d+>\s*)?(?:rocky[:,]?\s*)?"
    r"(?:(?:note|context|prep|brief|remember for today|for today|for my meeting|for the meeting|planning)\b|"
    r".*\b(?:use this|remember this|for my .*meeting|on the way to the office)\b)",
    re.IGNORECASE,
)
TASK_COMMAND_RE = re.compile(r"^\s*(?:<@!?\d+>\s*)?(?:rocky[:,]?\s*)?(remember|add task|create task|todo|mark .*done|done:|cancel task|forget task|remind me)\b", re.IGNORECASE)
SENSITIVE_RE = re.compile(r"(cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_meeting_context_note_capture(
    *,
    live: bool = False,
    acknowledge: bool = True,
    notification_dry_run: bool = False,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    state_file: str | Path = DEFAULT_STATE_FILE,
    ledger_db_path: str | Path | None = DEFAULT_LEDGER_DB,
    channel_ids: list[str] | None = None,
    since_minutes: int = 60,
    limit: int = 25,
    http_get: Any | None = None,
    post_func: Any | None = None,
    now: datetime | None = None,
    write_state: bool = True,
) -> dict[str, Any]:
    read_payload = read_discord_context_notes(
        config_path=config_path,
        state_file=state_file,
        channel_ids=channel_ids,
        since_minutes=since_minutes,
        limit=limit,
        http_get=http_get,
        now=now,
        write_state=bool(write_state and live),
    )
    if read_payload.get("status") not in {"ok", "degraded"}:
        return {
            **read_payload,
            "notes_recorded": 0,
            "ack_sent": 0,
            "ack_failed": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    ledger = MeetingContextNoteLedger(ledger_db_path)
    results: list[dict[str, Any]] = []
    ack_sent = 0
    ack_failed = 0
    for note in (read_payload.get("notes") or [])[: max(1, int(limit))]:
        fp = note_fingerprint(note)
        note["note_fingerprint"] = fp
        if not live:
            results.append({"source_ref": note.get("source_ref"), "note_fingerprint": fp, "status": "dry_run"})
            continue
        row = ledger.record_seen(note, status="seen", reason="discord_context_note_seen")
        ack = {"status": "skipped", "reason": "ack_disabled"}
        if acknowledge:
            ack = _acknowledge_note(note, ledger=ledger, notification_dry_run=notification_dry_run, post_func=post_func, config_path=Path(config_path))
            if ack.get("status") == "ack_sent":
                ack_sent += 1
            elif ack.get("status") == "ack_failed":
                ack_failed += 1
        results.append({
            "source_ref": row.get("source_ref"),
            "note_fingerprint": row.get("note_fingerprint"),
            "status": row.get("status"),
            "ack": ack,
            "preview": row.get("note_preview"),
        })
    status = "degraded" if ack_failed or read_payload.get("status") == "degraded" else "ok"
    return {
        "status": status,
        "reason": "meeting_context_note_capture_completed" if status == "ok" else "meeting_context_note_capture_degraded",
        "source_status": read_payload.get("status"),
        "note_count": read_payload.get("note_count", 0),
        "notes_recorded": sum(1 for item in results if item.get("status") not in {"dry_run"}),
        "ack_sent": ack_sent,
        "ack_failed": ack_failed,
        "results": results,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def read_discord_context_notes(
    *,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    state_file: str | Path = DEFAULT_STATE_FILE,
    channel_ids: list[str] | None = None,
    since_minutes: int = 60,
    limit: int = 25,
    http_get: Any | None = None,
    now: datetime | None = None,
    write_state: bool = True,
) -> dict[str, Any]:
    cfg = _load_config(Path(config_path))
    token = cfg.get("token") or os.getenv("DISCORD_TOKEN")
    if not token:
        return {"status": "blocked", "reason": "discord_token_missing", "notes": [], "note_count": 0}
    dusan_user_id = str(cfg.get("dusan_user_id") or "")
    channels = channel_ids or cfg.get("channel_ids") or []
    if not channels:
        return {"status": "blocked", "reason": "discord_channels_missing", "notes": [], "note_count": 0}
    state_path = Path(state_file)
    state = _read_json(state_path)
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(minutes=max(1, int(since_minutes)))
    getter = http_get or _discord_get
    notes: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    latest_by_channel: dict[str, str] = {}
    for channel_id in channels:
        try:
            params = {"limit": str(max(1, min(int(limit), 100)))}
            after = ((state.get("channels") or {}).get(str(channel_id)) or {}).get("last_message_id")
            if after:
                params["after"] = str(after)
            messages = getter(token, f"/channels/{channel_id}/messages?{parse.urlencode(params)}")
            for msg in reversed(messages if isinstance(messages, list) else []):
                if not _accepted_message(msg, dusan_user_id=dusan_user_id, cutoff=cutoff):
                    continue
                content = str(msg.get("content") or "")
                if not CONTEXT_NOTE_RE.search(content) or TASK_COMMAND_RE.search(content):
                    continue
                message_id = str(msg.get("id") or "")
                notes.append(
                    {
                        "source": "Discord",
                        "source_ref": f"discord-context:{channel_id}:{message_id}",
                        "text": _strip_rocky_prefix(content),
                        "created_at": str(msg.get("timestamp") or now_dt.isoformat()),
                        "target_date": _infer_target_date(content, now_dt=now_dt),
                        "channel_id": str(channel_id),
                        "message_id": message_id,
                        "author_id_hash": hash_text(str(((msg.get("author") or {}).get("id") or ""))),
                        "classification": "meeting_or_day_context",
                    }
                )
                if message_id:
                    latest_by_channel[str(channel_id)] = max(str(latest_by_channel.get(str(channel_id)) or ""), message_id)
        except Exception as exc:
            warnings.append({"channel_id_hash": hash_text(str(channel_id)), "reason": "discord_context_note_read_failed", "error_hash": hash_text(str(exc))})
    if latest_by_channel and write_state:
        _write_state(state_path, state, latest_by_channel)
    limited = notes[: max(1, int(limit))]
    return {
        "status": "ok" if not warnings else "degraded",
        "reason": None if not warnings else "some_discord_context_reads_failed",
        "notes": limited,
        "note_count": len(limited),
        "warning_count": len(warnings),
        "warnings": warnings,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _acknowledge_note(note: dict[str, Any], *, ledger: MeetingContextNoteLedger, notification_dry_run: bool, post_func: Any | None, config_path: Path) -> dict[str, Any]:
    source_ref = str(note.get("source_ref") or "")
    fp = str(note.get("note_fingerprint") or note_fingerprint(note))
    existing = ledger.get(source_ref=source_ref, note_fingerprint=fp) or {}
    if existing.get("ack_status") == "ack_sent":
        return {"status": "skipped", "reason": "ack_already_sent"}
    message = f"Got it. I will use this for today's planning and meeting prep: {safe_text_value(note.get('text'), 90)}"
    if notification_dry_run:
        return {"status": "dry_run", "reason": "notification_dry_run", "message_preview": message[:200], "message_sha256": hash_text(message)}
    try:
        token = (_read_json(config_path).get("channels") or {}).get("discord", {}).get("token") or os.getenv("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("discord_token_missing")
        poster = post_func or _post_discord
        delivery = poster(token=token, channel_id=str(note.get("channel_id") or ""), content=message)
    except Exception as exc:
        ledger.update_ack(source_ref=source_ref, note_fingerprint=fp, status="ack_failed", reason=exc.__class__.__name__)
        return {"status": "ack_failed", "reason": exc.__class__.__name__, "error_hash": hash_text(str(exc))}
    if str(delivery.get("status") or "posted") in {"posted", "ok", "success"}:
        ledger.update_ack(source_ref=source_ref, note_fingerprint=fp, status="ack_sent", message_id=(delivery.get("message_ids") or [None])[0])
        return {"status": "ack_sent", "message_ids": delivery.get("message_ids") or []}
    ledger.update_ack(source_ref=source_ref, note_fingerprint=fp, status="ack_failed", reason=str(delivery.get("reason") or "discord_post_failed"))
    return {"status": "ack_failed", "reason": str(delivery.get("reason") or "discord_post_failed"), "error_hash": delivery.get("error_hash")}


def _post_discord(*, token: str, channel_id: str, content: str) -> dict[str, Any]:
    req = request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
        return {"status": "posted", "message_ids": [body.get("id")], "channel_id": channel_id}


def _accepted_message(msg: dict[str, Any], *, dusan_user_id: str, cutoff: datetime) -> bool:
    author = msg.get("author") or {}
    if author.get("bot"):
        return False
    if dusan_user_id and str(author.get("id") or "") != dusan_user_id:
        return False
    timestamp = _parse_dt(msg.get("timestamp"))
    return bool(timestamp is None or timestamp >= cutoff)


def _discord_get(token: str, path: str) -> Any:
    req = request.Request(f"{DISCORD_API_BASE}{path}", headers={"Authorization": f"Bot {token}", "User-Agent": "RockyMeetingContextNoteCapture/1.0"}, method="GET")
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _strip_rocky_prefix(value: str) -> str:
    text = re.sub(r"^\s*(?:<@!?\d+>\s*)?(?:rocky[:,]?\s*)?", "", str(value or ""), flags=re.IGNORECASE).strip()
    return safe_text_value(text, 1200)


def _infer_target_date(value: str, *, now_dt: datetime) -> str:
    text = str(value or "").lower()
    if "tomorrow" in text:
        return (now_dt + timedelta(days=1)).date().isoformat()
    return now_dt.date().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, state: dict[str, Any], latest_by_channel: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = dict(state.get("channels") or {})
    for channel_id, message_id in latest_by_channel.items():
        channels[channel_id] = {"last_message_id": message_id, "updated_at": datetime.now(timezone.utc).isoformat()}
    state["channels"] = channels
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Dusan Discord context notes for Rocky.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--no-ack", action="store_true", dest="no_ack")
    parser.add_argument("--since-minutes", type=int, default=60, dest="since_minutes")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--ledger-db", dest="ledger_db")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = run_meeting_context_note_capture(live=args.live, acknowledge=not args.no_ack, notification_dry_run=args.notification_dry_run, since_minutes=args.since_minutes, limit=args.limit, ledger_db_path=args.ledger_db)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting context notes captured: {payload.get('notes_recorded', 0)}")
    return 0 if payload.get("status") in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
