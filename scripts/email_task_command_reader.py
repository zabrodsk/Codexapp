#!/usr/bin/env python3
"""Read trusted email-to-Rocky task command mirrors from local inbox files."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_EMAIL_COMMAND_DIR = Path("/Users/clawdbot/.openclaw/inbox/task-commands/email")
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def read_email_task_commands(
    *,
    inbox_dir: str | Path = DEFAULT_EMAIL_COMMAND_DIR,
    since_minutes: int = 60,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(inbox_dir).expanduser()
    if not root.exists():
        return {"status": "ok", "commands": [], "command_count": 0, "reason": "email_command_inbox_missing"}
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = now_dt - timedelta(minutes=max(1, int(since_minutes)))
    commands: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff - timedelta(days=1):
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                created = _parse_dt(item.get("created_at")) or now_dt
                if created < cutoff:
                    continue
                commands.append(_safe_command(item))
        except Exception as exc:
            warnings.append({"path_hash": _hash_text(str(path)), "reason": "email_command_file_failed", "error_hash": _hash_text(str(exc))})
        if len(commands) >= max(1, int(limit)):
            break
    commands.sort(key=lambda item: str(item.get("created_at") or ""), reverse=False)
    limited = commands[: max(1, int(limit))]
    return {
        "status": "ok" if not warnings else "degraded",
        "commands": limited,
        "command_count": len(limited),
        "warnings": warnings,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _safe_command(item: dict[str, Any]) -> dict[str, Any]:
    message_id = str(item.get("message_id") or item.get("messageId") or "")
    thread_id = str(item.get("thread_id") or item.get("threadId") or "")
    source_ref = str(item.get("source_ref") or f"agentmail:{_hash_text(message_id or thread_id or json.dumps(item, sort_keys=True, default=str))}")
    text = _safe_text(item.get("text") or item.get("command") or item.get("body_preview") or "", 1200)
    return {
        "source": "Command",
        "source_channel": "email",
        "source_ref": source_ref,
        "text": text,
        "created_at": item.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "message_id_hash": _hash_text(message_id),
        "thread_id_hash": _hash_text(thread_id),
        "sender_hash": _hash_text(str(item.get("sender") or "")),
    }


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{_hash_text(text)}]"
    return text[:limit]


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
