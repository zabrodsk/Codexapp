#!/usr/bin/env python3
"""Discord acknowledgement helper for trusted direct task commands."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib import error, request

from task_command_ledger import TaskCommandLedger, command_fingerprint, safe_preview

DEFAULT_OPENCLAW_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/openclaw.json")
DISCORD_API = "https://discord.com/api/v10"
ACK_STATUSES = {"created", "updated", "skipped"}


def maybe_acknowledge_discord_command(
    command: dict[str, Any],
    result: dict[str, Any],
    *,
    ledger: TaskCommandLedger,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    post_func: Any | None = None,
) -> dict[str, Any]:
    if str(command.get("source_channel") or "").lower() != "discord":
        return {"status": "skipped", "reason": "ack_not_supported_for_source", "ack_attempted": False}
    if result.get("status") not in ACK_STATUSES:
        return {"status": "skipped", "reason": "ack_not_needed_for_status", "ack_attempted": False}
    channel_id = str(command.get("channel_id") or "")
    if not channel_id:
        return {"status": "skipped", "reason": "discord_channel_missing", "ack_attempted": False}
    source_ref = str(command.get("source_ref") or "")
    fingerprint = str(command.get("command_fingerprint") or command_fingerprint(command))
    row = ledger.get(source_ref=source_ref, command_fingerprint=fingerprint)
    if row and row.get("ack_status") == "ack_sent":
        return {"status": "skipped", "reason": "ack_already_sent", "ack_attempted": False}
    content = render_task_ack(command, result)
    try:
        token = _load_discord_token(Path(config_path))
        poster = post_func or _post_to_discord
        delivery = poster(token=token, channel_id=channel_id, content=content)
    except Exception as exc:
        delivery = {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc))}
    if str(delivery.get("status")) in {"posted", "ok", "success"}:
        message_ids = delivery.get("message_ids") or []
        ledger.update_ack(source_ref=source_ref, command_fingerprint=fingerprint, status="ack_sent", message_id=str(message_ids[0]) if message_ids else None)
        return {
            "status": "ack_sent",
            "reason": "discord_ack_sent",
            "ack_attempted": True,
            "message_sha256": _hash_text(content),
            "delivery": _safe_delivery(delivery),
        }
    ledger.update_ack(source_ref=source_ref, command_fingerprint=fingerprint, status="ack_failed", reason=str(delivery.get("reason") or "ack_failed"))
    return {
        "status": "ack_failed",
        "reason": str(delivery.get("reason") or "ack_failed"),
        "ack_attempted": True,
        "message_sha256": _hash_text(content),
        "delivery": _safe_delivery(delivery),
    }


def render_task_ack(command: dict[str, Any], result: dict[str, Any]) -> str:
    task = result.get("task") or {}
    title = safe_preview(task.get("title") or command.get("text") or "task", limit=120)
    if result.get("status") == "skipped":
        return f"Already tracked: {title}"
    if result.get("status") == "updated":
        return f"Got it. Updated task: {title}"
    return f"Got it. Added task: {title}"


def _load_discord_token(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = (((payload.get("channels") or {}).get("discord") or {}).get("token") or "").strip()
    if not token:
        raise RuntimeError("discord_token_missing")
    return token


def _post_to_discord(*, token: str, channel_id: str, content: str) -> dict[str, Any]:
    req = request.Request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {"status": "posted", "channel_id": channel_id, "message_ids": [body.get("id")]}
    except error.HTTPError as exc:
        return {"status": "failed", "reason": f"discord_http_{exc.code}", "error_hash": _hash_text(str(exc))}


def _safe_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": delivery.get("status"),
        "reason": delivery.get("reason"),
        "channel_id": delivery.get("channel_id"),
        "message_ids": delivery.get("message_ids"),
        "error_hash": delivery.get("error_hash"),
    }


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
