#!/usr/bin/env python3
"""Read explicit Rocky task commands from approved Discord contexts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import parse

from assistant_notification_dispatcher import DEFAULT_OPENCLAW_BIN


DEFAULT_OPENCLAW_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/openclaw.json")
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/discord_task_command_reader.json")
COMMAND_RE = re.compile(
    r"^\s*(?:<@!?\d+>\s*)?(?:rocky[:,]?\s*)?(remember|add task|create task|todo|mark .*done|done:|cancel task|forget task|remind me)\b",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def read_discord_task_commands(
    *,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    state_file: str | Path = DEFAULT_STATE_FILE,
    channel_ids: list[str] | None = None,
    since_minutes: int = 10,
    limit: int = 25,
    http_get: Any | None = None,
    now: datetime | None = None,
    write_state: bool = True,
) -> dict[str, Any]:
    cfg = _load_config(Path(config_path))
    token = cfg.get("token") or os.getenv("DISCORD_TOKEN")
    if not token:
        return {"status": "blocked", "reason": "discord_token_missing", "commands": [], "command_count": 0}
    dusan_user_id = str(cfg.get("dusan_user_id") or "")
    channels = channel_ids or cfg.get("channel_ids") or []
    if not channels:
        return {"status": "blocked", "reason": "discord_channels_missing", "commands": [], "command_count": 0}
    state_path = Path(state_file)
    state = _read_json(state_path)
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(minutes=max(1, int(since_minutes)))
    getter = http_get or _openclaw_discord_read
    commands: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    latest_by_channel: dict[str, str] = {}
    for channel_id in channels:
        try:
            params = {"limit": str(max(1, min(int(limit), 100)))}
            after = ((state.get("channels") or {}).get(str(channel_id)) or {}).get("last_message_id")
            if after:
                params["after"] = str(after)
            if http_get:
                messages = getter(token, f"/channels/{channel_id}/messages?{parse.urlencode(params)}")
            else:
                messages = getter(channel_id=str(channel_id), limit=max(1, min(int(limit), 100)))
                if after:
                    messages = [msg for msg in messages if _message_id_after(str(msg.get("id") or ""), str(after))]
            for msg in reversed(messages if isinstance(messages, list) else []):
                if not _accepted_message(msg, dusan_user_id=dusan_user_id, cutoff=cutoff):
                    continue
                content = str(msg.get("content") or "")
                if not COMMAND_RE.search(content):
                    continue
                message_id = str(msg.get("id") or "")
                commands.append(
                    {
                        "source": "Discord",
                        "source_channel": "discord",
                        "source_ref": f"discord:{channel_id}:{message_id}",
                        "text": _safe_text(content, 1200),
                        "created_at": str(msg.get("timestamp") or now_dt.isoformat()),
                        "channel_id": str(channel_id),
                        "message_id": message_id,
                        "author_id_hash": _hash_text(str(((msg.get("author") or {}).get("id") or ""))),
                    }
                )
                if message_id:
                    latest_by_channel[str(channel_id)] = max(str(latest_by_channel.get(str(channel_id)) or ""), message_id)
        except Exception as exc:
            warnings.append({"channel_id_hash": _hash_text(str(channel_id)), "reason": "discord_command_read_failed", "error_hash": _hash_text(str(exc))})
    if latest_by_channel and write_state:
        _write_state(state_path, state, latest_by_channel)
    limited = commands[: max(1, int(limit))]
    return {
        "status": "ok" if not warnings else "degraded",
        "commands": limited,
        "command_count": len(limited),
        "warning_count": len(warnings),
        "warnings": warnings,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _accepted_message(msg: dict[str, Any], *, dusan_user_id: str, cutoff: datetime) -> bool:
    author = msg.get("author") or {}
    if author.get("bot"):
        return False
    if dusan_user_id and str(author.get("id") or "") != dusan_user_id:
        return False
    timestamp = _parse_dt(msg.get("timestamp"))
    return bool(timestamp is None or timestamp >= cutoff)


def _openclaw_discord_read(*, channel_id: str, limit: int = 25) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [
            str(DEFAULT_OPENCLAW_BIN),
            "message",
            "read",
            "--channel",
            "discord",
            "--target",
            f"channel:{channel_id}",
            "--json",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"openclaw_discord_read_failed:{_hash_text(output)}")
    payload = json.loads(proc.stdout or "{}")
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    messages = inner.get("messages") or []
    return messages[: max(1, min(int(limit), 100))] if isinstance(messages, list) else []


def _message_id_after(message_id: str, after: str) -> bool:
    try:
        return int(message_id) > int(after)
    except Exception:
        return message_id > after


def _load_config(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    discord = (data.get("channels") or {}).get("discord") or {}
    guilds = discord.get("guilds") or {}
    channel_ids: list[str] = []
    for guild in guilds.values() if isinstance(guilds, dict) else []:
        for cid, cfg in ((guild.get("channels") or {}).items() if isinstance(guild, dict) else []):
            if cfg.get("requireMention") or cfg.get("autoThread") or str(cid) in set(discord.get("allowFrom") or []):
                channel_ids.append(str(cid))
    return {
        "token": discord.get("token"),
        "dusan_user_id": ((data.get("agentmail") or {}).get("approverDiscordUserId") or "1484630539477717182"),
        "channel_ids": sorted(set(channel_ids)),
    }


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
