#!/usr/bin/env python3
"""Failure-only notification helper for Rocky assistant workflows."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_scheduler_state import AssistantSchedulerState


DEFAULT_ALERT_CHANNEL_ID = "1485710572325703901"
DEFAULT_OPENCLAW_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/openclaw.json")
POLICY_VERSION = "rocky-notification-policy-v1"
WORKFLOW = "assistant_notification_dispatcher"
DISCORD_API = "https://discord.com/api/v10"
ATTENTION_STATUSES = {
    "blocked",
    "failed",
    "manual_review_required",
    "recovery_needed",
}
SAFE_NOISE_STATUSES = {
    "created",
    "ok",
    "skipped_duplicate",
    "skipped_no_workout",
    "skipped_weekend_target",
    "source_ref_drift_verified",
}
SENSITIVE_KEY_RE = re.compile(
    r"(auth|body|coach|content|cookie|credential|description|html|notes|password|raw|secret|token|transcript|webcal)",
    re.IGNORECASE,
)
SENSITIVE_TEXT_RE = re.compile(
    r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth)",
    re.IGNORECASE,
)


def should_notify(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "")
    reason = str(payload.get("reason") or "")
    if status in SAFE_NOISE_STATUSES:
        return False
    if status in ATTENTION_STATUSES:
        return True
    return "manual_review" in reason or "blocked" in reason or "failed" in reason


def dispatch_failure_notification(
    payload: dict[str, Any],
    *,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    ledger_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    dry_run: bool = False,
    post_func=None,
) -> dict[str, Any]:
    safe_payload = _redact(payload)
    idempotency_key = _notification_key(safe_payload)
    if not should_notify(safe_payload):
        return {
            "status": "skipped",
            "reason": "notification_not_needed",
            "idempotency_key": idempotency_key,
            "channel_id": channel_id,
            "notification_attempted": False,
        }

    message = render_notification(safe_payload)
    if dry_run:
        return {
            "status": "dry_run",
            "reason": "notification_dry_run",
            "idempotency_key": idempotency_key,
            "channel_id": channel_id,
            "message_preview": message[:500],
            "message_sha256": _hash_text(message),
            "notification_attempted": False,
            "payload": safe_payload,
        }

    try:
        token = _load_discord_token(Path(config_path))
        poster = post_func or _post_to_discord
        delivery = poster(token=token, channel_id=channel_id, content=message)
    except Exception as exc:
        delivery = {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc))}

    delivery_status = str(delivery.get("status") or "failed")
    audit_log = AssistantAuditLog(ledger_path)
    if delivery_status in {"ok", "posted", "success"}:
        event = audit_log.record_event(
            event_type="assistant.notification_sent",
            workflow=WORKFLOW,
            idempotency_key=idempotency_key,
            policy_version=POLICY_VERSION,
            decision="completed",
            reason="failure_notification_sent",
            sources=["assistant-notification:discord"],
            artifacts={
                "channel_id": channel_id,
                "message_sha256": _hash_text(message),
                "message_chars": len(message),
                "delivery": _safe_delivery(delivery),
            },
        )
        return {
            "status": "posted",
            "reason": "failure_notification_sent",
            "audit_id": event.audit_id,
            "idempotency_key": idempotency_key,
            "channel_id": channel_id,
            "notification_attempted": True,
            "message_sha256": _hash_text(message),
            "delivery": _safe_delivery(delivery),
        }

    state = AssistantSchedulerState(scheduler_db_path)
    dead = state.upsert_dead_letter(
        job_name="assistant_notification_dispatcher",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        failure_class="assistant_notification_failed",
        safe_summary=str(delivery.get("reason") or "notification delivery failed"),
        source_refs=["assistant-notification:discord"],
        recovery_hint="Inspect Discord token/config and retry the failed assistant notification if still relevant.",
        error_hash=delivery.get("error_hash") or _hash_text(json.dumps(_safe_delivery(delivery), sort_keys=True)),
    )
    event = audit_log.record_event(
        event_type="assistant.notification_failed",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision="failed",
        reason=str(delivery.get("reason") or "notification_delivery_failed"),
        sources=["assistant-notification:discord"],
        artifacts={"channel_id": channel_id, "delivery": _safe_delivery(delivery), "dead_letter": dead},
    )
    return {
        "status": "failed",
        "reason": str(delivery.get("reason") or "notification_delivery_failed"),
        "audit_id": event.audit_id,
        "idempotency_key": idempotency_key,
        "channel_id": channel_id,
        "notification_attempted": True,
        "delivery": _safe_delivery(delivery),
        "dead_letter": dead,
    }


def render_notification(payload: dict[str, Any]) -> str:
    workflow = str(payload.get("workflow") or payload.get("job_name") or "assistant workflow").replace("_", " ")
    lines = [
        f"Rocky {workflow} needs attention",
        f"Status: {payload.get('status')}",
        f"Reason: {payload.get('reason')}",
    ]
    if payload.get("target_date"):
        lines.append(f"Target date: {payload.get('target_date')}")
    if payload.get("idempotency_key"):
        lines.append(f"Idempotency key: {payload.get('idempotency_key')}")
    if payload.get("recommended_action"):
        lines.append(f"Action: {payload.get('recommended_action')}")
    return "\n".join(lines)


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


def _notification_key(payload: dict[str, Any]) -> str:
    raw = {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "target_date": payload.get("target_date"),
        "idempotency_key": payload.get("idempotency_key"),
    }
    return f"assistant-notification:{_hash_text(json.dumps(raw, sort_keys=True, default=str))}"


def _safe_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    return _redact(
        {
            "status": delivery.get("status"),
            "reason": delivery.get("reason"),
            "channel_id": delivery.get("channel_id"),
            "message_ids": delivery.get("message_ids"),
            "error_hash": delivery.get("error_hash"),
        }
    )


def _redact(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and SENSITIVE_KEY_RE.search(parent_key):
        return _redacted_value(value)
    if isinstance(value, dict):
        return {str(key): _redact(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        return _redacted_value(value)
    return value


def _redacted_value(value: Any) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return {"redacted": True, "sha256": _hash_text(text), "chars": len(text)}


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch safe Rocky assistant failure notifications.")
    parser.add_argument("--status", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--target-date", dest="target_date")
    parser.add_argument("--idempotency-key", dest="idempotency_key")
    parser.add_argument("--channel-id", default=DEFAULT_ALERT_CHANNEL_ID, dest="channel_id")
    parser.add_argument("--config-path", default=str(DEFAULT_OPENCLAW_CONFIG_PATH), dest="config_path")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = dispatch_failure_notification(
        {
            "status": args.status,
            "reason": args.reason,
            "target_date": args.target_date,
            "idempotency_key": args.idempotency_key,
        },
        channel_id=args.channel_id,
        config_path=args.config_path,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        dry_run=args.dry_run,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Notification: {payload.get('status')} {payload.get('reason')}")
    return 0 if payload.get("status") in {"posted", "dry_run", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
