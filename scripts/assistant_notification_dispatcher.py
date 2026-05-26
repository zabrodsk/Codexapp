#!/usr/bin/env python3
"""Notification routing for Rocky assistant workflows.

Discord is the primary delivery channel. AgentMail email is the fallback for
critical assistant alerts so failures remain visible even when Discord
permissions drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agentmail_bridge_health import build_agentmail_bridge_health
from assistant_audit_log import AssistantAuditLog
from assistant_scheduler_state import AssistantSchedulerState


DEFAULT_ALERT_CHANNEL_ID = "1485710572325703901"
DEFAULT_FALLBACK_EMAIL = "dusan.zabrodsky@rockaway.cz"
DEFAULT_OPENCLAW_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/openclaw.json")
DEFAULT_AGENTMAIL_CONFIG_PATH = Path("/Users/clawdbot/.openclaw/agentmail-bridge/config.json")
DEFAULT_AGENTMAIL_CREDENTIALS_PATH = Path("/Users/clawdbot/.openclaw/credentials/agentmail.json")
DEFAULT_AGENTMAIL_BRIDGE_ROOT = Path("/Users/clawdbot/.openclaw/agentmail-bridge")
POLICY_VERSION = "rocky-notification-policy-v2"
WORKFLOW = "assistant_notification_dispatcher"
DISCORD_API = "https://discord.com/api/v10"
ATTENTION_STATUSES = {
    "blocked",
    "failed",
    "manual_review_required",
    "recovery_needed",
    "waiting_for_user",
}
SAFE_NOISE_STATUSES = {
    "created",
    "ok",
    "posted",
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
    r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])",
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
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    agentmail_config_path: str | Path = DEFAULT_AGENTMAIL_CONFIG_PATH,
    agentmail_credentials_path: str | Path = DEFAULT_AGENTMAIL_CREDENTIALS_PATH,
    ledger_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    dry_run: bool = False,
    post_func=None,
    agentmail_send_func=None,
    allow_agentmail_fallback: bool = True,
) -> dict[str, Any]:
    return dispatch_assistant_notification(
        payload,
        channel_id=channel_id,
        fallback_email=fallback_email,
        config_path=config_path,
        agentmail_config_path=agentmail_config_path,
        agentmail_credentials_path=agentmail_credentials_path,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        dry_run=dry_run,
        post_func=post_func,
        agentmail_send_func=agentmail_send_func,
        allow_agentmail_fallback=allow_agentmail_fallback,
    )


def dispatch_user_notification(
    *,
    workflow: str,
    message: str,
    subject: str,
    reason: str = "assistant_user_notification",
    target_date: str | None = None,
    idempotency_key: str | None = None,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    agentmail_config_path: str | Path = DEFAULT_AGENTMAIL_CONFIG_PATH,
    agentmail_credentials_path: str | Path = DEFAULT_AGENTMAIL_CREDENTIALS_PATH,
    ledger_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    dry_run: bool = False,
    post_func=None,
    agentmail_send_func=None,
) -> dict[str, Any]:
    payload = {
        "workflow": workflow,
        "status": "manual_review_required",
        "reason": reason,
        "target_date": target_date,
        "idempotency_key": idempotency_key,
    }
    return dispatch_assistant_notification(
        payload,
        message=message,
        subject=subject,
        channel_id=channel_id,
        fallback_email=fallback_email,
        config_path=config_path,
        agentmail_config_path=agentmail_config_path,
        agentmail_credentials_path=agentmail_credentials_path,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        dry_run=dry_run,
        post_func=post_func,
        agentmail_send_func=agentmail_send_func,
        allow_agentmail_fallback=True,
        force_notify=True,
    )


def dispatch_assistant_notification(
    payload: dict[str, Any],
    *,
    message: str | None = None,
    subject: str | None = None,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    agentmail_config_path: str | Path = DEFAULT_AGENTMAIL_CONFIG_PATH,
    agentmail_credentials_path: str | Path = DEFAULT_AGENTMAIL_CREDENTIALS_PATH,
    ledger_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    dry_run: bool = False,
    post_func=None,
    agentmail_send_func=None,
    allow_agentmail_fallback: bool = True,
    force_notify: bool = False,
) -> dict[str, Any]:
    safe_payload = _redact(payload)
    idempotency_key = _notification_key(safe_payload)
    if not force_notify and not should_notify(safe_payload):
        return {
            "status": "skipped",
            "final_status": "skipped",
            "reason": "notification_not_needed",
            "idempotency_key": idempotency_key,
            "channel_id": channel_id,
            "deliveries": [],
            "notification_attempted": False,
        }

    safe_message = _safe_message(message or render_notification(safe_payload))
    safe_subject = _safe_subject(subject or render_subject(safe_payload))
    message_hash = _hash_text(safe_message)
    if dry_run:
        return {
            "status": "dry_run",
            "final_status": "dry_run",
            "reason": "notification_dry_run",
            "idempotency_key": idempotency_key,
            "channel_id": channel_id,
            "fallback_email": fallback_email,
            "message_preview": safe_message[:500],
            "message_sha256": message_hash,
            "deliveries": [
                {"channel": "discord", "status": "dry_run", "channel_id": channel_id},
                *([{"channel": "agentmail_email", "status": "dry_run", "to": fallback_email}] if allow_agentmail_fallback else []),
            ],
            "fallback_used": False,
            "notification_attempted": False,
            "payload": safe_payload,
        }

    deliveries: list[dict[str, Any]] = []
    discord_delivery = _deliver_discord(
        channel_id=channel_id,
        content=safe_message,
        config_path=Path(config_path),
        post_func=post_func,
    )
    deliveries.append(discord_delivery)
    if discord_delivery.get("status") == "posted":
        return _record_notification_success(
            idempotency_key=idempotency_key,
            ledger_path=ledger_path,
            channel_id=channel_id,
            message_hash=message_hash,
            message_chars=len(safe_message),
            deliveries=deliveries,
            fallback_used=False,
            reason="notification_sent",
        )

    fallback_used = False
    fallback_allowed = allow_agentmail_fallback and not (post_func is not None and agentmail_send_func is None)
    if fallback_allowed:
        fallback_used = True
        fallback_text = _fallback_email_text(safe_message, discord_delivery)
        agentmail_delivery = _deliver_agentmail(
            to_email=fallback_email,
            subject=safe_subject,
            text=fallback_text,
            config_path=Path(agentmail_config_path),
            credentials_path=Path(agentmail_credentials_path),
            send_func=agentmail_send_func,
        )
        deliveries.append(agentmail_delivery)
        if agentmail_delivery.get("status") == "posted":
            return _record_notification_success(
                idempotency_key=idempotency_key,
                ledger_path=ledger_path,
                channel_id=channel_id,
                message_hash=message_hash,
                message_chars=len(safe_message),
                deliveries=deliveries,
                fallback_used=True,
                reason="notification_sent_with_agentmail_fallback",
            )

    primary_reason = str(discord_delivery.get("reason") or "discord_delivery_failed")
    final_reason = str((deliveries[-1] if deliveries else {}).get("reason") or primary_reason)
    state = AssistantSchedulerState(scheduler_db_path)
    dead = state.upsert_dead_letter(
        job_name="assistant_notification_dispatcher",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        failure_class="assistant_notification_failed",
        safe_summary=final_reason,
        source_refs=["assistant-notification:discord", *("assistant-notification:agentmail" for _ in [1] if fallback_used)],
        recovery_hint="Inspect Discord permissions and AgentMail outbound readiness; retry the notification if still relevant.",
        error_hash=_hash_text(json.dumps(_safe_delivery({"deliveries": deliveries}), sort_keys=True)),
    )
    event = AssistantAuditLog(ledger_path).record_event(
        event_type="assistant.notification_failed",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision="failed",
        reason=final_reason,
        sources=["assistant-notification:router"],
        artifacts={"channel_id": channel_id, "fallback_email": fallback_email, "deliveries": deliveries, "dead_letter": dead},
    )
    return {
        "status": "failed",
        "final_status": "failed",
        "reason": final_reason,
        "audit_id": event.audit_id,
        "idempotency_key": idempotency_key,
        "channel_id": channel_id,
        "fallback_email": fallback_email,
        "primary_failure_reason": primary_reason,
        "fallback_used": fallback_used,
        "message_sha256": message_hash,
        "notification_attempted": True,
        "deliveries": deliveries,
        "dead_letter": dead,
    }


def build_notification_health(
    *,
    channel_id: str = DEFAULT_ALERT_CHANNEL_ID,
    fallback_email: str = DEFAULT_FALLBACK_EMAIL,
    config_path: str | Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    agentmail_config_path: str | Path = DEFAULT_AGENTMAIL_CONFIG_PATH,
    agentmail_credentials_path: str | Path = DEFAULT_AGENTMAIL_CREDENTIALS_PATH,
    check_discord: bool = True,
    ledger_path: str | Path | None = None,
    write_audit: bool = True,
) -> dict[str, Any]:
    discord = _discord_health(channel_id=channel_id, config_path=Path(config_path), check_discord=check_discord)
    agentmail = _agentmail_health(
        fallback_email=fallback_email,
        config_path=Path(agentmail_config_path),
        credentials_path=Path(agentmail_credentials_path),
    )
    status = "ok" if discord.get("status") == "ok" or agentmail.get("status") == "ok" else "blocked"
    payload = _redact({
        "status": status,
        "reason": "notification_health_ok" if status == "ok" else "notification_delivery_paths_blocked",
        "discord": discord,
        "agentmail": agentmail,
        "fallback_email": fallback_email,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
        "notification_sent": False,
    })
    if write_audit:
        AssistantAuditLog(ledger_path).record_event(
            event_type="assistant.notification_health_checked",
            workflow=WORKFLOW,
            idempotency_key=f"assistant-notification-health:{_hash_text(channel_id + fallback_email)}",
            policy_version=POLICY_VERSION,
            decision="allowed" if status == "ok" else "blocked",
            reason=str(payload.get("reason")),
            sources=["assistant-notification:health"],
            artifacts=payload,
        )
    return payload


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


def render_subject(payload: dict[str, Any]) -> str:
    workflow = str(payload.get("workflow") or payload.get("job_name") or "assistant workflow").replace("_", " ")
    reason = str(payload.get("reason") or payload.get("status") or "needs attention").replace("_", " ")
    return _safe_subject(f"Rocky needs attention: {workflow} - {reason}")


def _deliver_discord(*, channel_id: str, content: str, config_path: Path, post_func=None) -> dict[str, Any]:
    try:
        token = _load_discord_token(config_path)
        poster = post_func or _post_to_discord
        delivery = poster(token=token, channel_id=channel_id, content=content)
    except Exception as exc:
        delivery = {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc))}
    status = "posted" if delivery.get("status") in {"ok", "posted", "success"} else "failed"
    return _safe_delivery({
        "channel": "discord",
        "status": status,
        "reason": _classify_reason(str(delivery.get("reason") or "")) if status == "failed" else "posted",
        "channel_id": channel_id,
        "message_ids": delivery.get("message_ids"),
        "error_hash": delivery.get("error_hash"),
    })


def _deliver_agentmail(
    *,
    to_email: str,
    subject: str,
    text: str,
    config_path: Path,
    credentials_path: Path,
    send_func=None,
) -> dict[str, Any]:
    try:
        sender = send_func or _send_agentmail_email
        delivery = sender(to_email=to_email, subject=subject, text=text, config_path=config_path, credentials_path=credentials_path)
    except Exception as exc:
        delivery = {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc))}
    status = "posted" if delivery.get("status") in {"ok", "posted", "sent", "success"} else "failed"
    return _safe_delivery({
        "channel": "agentmail_email",
        "status": status,
        "reason": delivery.get("reason") or ("posted" if status == "posted" else "agentmail_send_failed"),
        "to": to_email,
        "message_ids": delivery.get("message_ids"),
        "error_hash": delivery.get("error_hash"),
    })


def _record_notification_success(
    *,
    idempotency_key: str,
    ledger_path: str | Path | None,
    channel_id: str,
    message_hash: str,
    message_chars: int,
    deliveries: list[dict[str, Any]],
    fallback_used: bool,
    reason: str,
) -> dict[str, Any]:
    event = AssistantAuditLog(ledger_path).record_event(
        event_type="assistant.notification_sent",
        workflow=WORKFLOW,
        idempotency_key=idempotency_key,
        policy_version=POLICY_VERSION,
        decision="completed",
        reason=reason,
        sources=["assistant-notification:router"],
        artifacts={
            "channel_id": channel_id,
            "message_sha256": message_hash,
            "message_chars": message_chars,
            "deliveries": deliveries,
            "fallback_used": fallback_used,
        },
    )
    return {
        "status": "posted",
        "final_status": "posted",
        "reason": reason,
        "audit_id": event.audit_id,
        "idempotency_key": idempotency_key,
        "channel_id": channel_id,
        "notification_attempted": True,
        "message_sha256": message_hash,
        "deliveries": deliveries,
        "fallback_used": fallback_used,
        "primary_failure_reason": None if not fallback_used else next((d.get("reason") for d in deliveries if d.get("channel") == "discord"), None),
    }


def _send_agentmail_email(*, to_email: str, subject: str, text: str, config_path: Path, credentials_path: Path) -> dict[str, Any]:
    bridge_root = DEFAULT_AGENTMAIL_BRIDGE_ROOT
    if not bridge_root.exists():
        return {"status": "failed", "reason": "agentmail_bridge_missing"}
    node_script = r"""
import fs from 'node:fs';
import { AgentMailClient } from 'agentmail';
const input = JSON.parse(fs.readFileSync(0, 'utf-8'));
const config = JSON.parse(fs.readFileSync(input.configPath, 'utf-8'));
const creds = JSON.parse(fs.readFileSync(input.credentialsPath, 'utf-8'));
const inbox = creds.inboxId || config.inbox;
const client = new AgentMailClient({ apiKey: creds.apiKey });
const response = await client.inboxes.messages.send(inbox, {
  to: input.to,
  subject: input.subject,
  text: input.text,
});
console.log(JSON.stringify({
  status: 'posted',
  messageId: response?.messageId || response?.message_id || response?.id || null,
}));
"""
    payload = {
        "to": to_email,
        "subject": subject,
        "text": text,
        "configPath": str(config_path),
        "credentialsPath": str(credentials_path),
    }
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(bridge_root),
        timeout=30,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return {"status": "failed", "reason": "agentmail_send_failed", "error_hash": _hash_text(output)}
    try:
        parsed = json.loads(proc.stdout or "{}")
    except Exception:
        parsed = {}
    return {"status": "posted", "message_ids": [parsed.get("messageId")] if parsed.get("messageId") else [], "error_hash": None}


def _discord_health(*, channel_id: str, config_path: Path, check_discord: bool) -> dict[str, Any]:
    try:
        token = _load_discord_token(config_path)
    except Exception as exc:
        return {"status": "blocked", "reason": exc.__class__.__name__, "token_configured": False}
    if not check_discord:
        return {"status": "ok", "reason": "discord_token_configured", "token_configured": True, "channel_id": channel_id}
    req = request.Request(
        f"{DISCORD_API}/channels/{channel_id}",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            return {"status": "ok", "reason": "discord_channel_access_ok", "channel_id": channel_id, "http_status": response.status}
    except error.HTTPError as exc:
        return {"status": "blocked", "reason": _classify_reason(f"discord_http_{exc.code}"), "channel_id": channel_id, "http_status": exc.code}
    except Exception as exc:
        return {"status": "blocked", "reason": exc.__class__.__name__, "channel_id": channel_id, "error_hash": _hash_text(str(exc))}


def _agentmail_health(*, fallback_email: str, config_path: Path, credentials_path: Path) -> dict[str, Any]:
    bridge = build_agentmail_bridge_health(run_tests=False, read_launchctl=True)
    config_ok = config_path.is_file()
    creds_ok = credentials_path.is_file()
    inbox = None
    try:
        if config_ok:
            inbox = json.loads(config_path.read_text(encoding="utf-8")).get("inbox")
    except Exception:
        pass
    status = "ok" if bridge.get("status") in {"ok", "degraded"} and config_ok and creds_ok and inbox else "blocked"
    return {
        "status": status,
        "reason": "agentmail_outbound_ready" if status == "ok" else "agentmail_outbound_not_ready",
        "bridge_status": bridge.get("status"),
        "config_present": config_ok,
        "credentials_present": creds_ok,
        "inbox_configured": bool(inbox),
        "fallback_email": fallback_email,
    }


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
            "channel": delivery.get("channel"),
            "status": delivery.get("status"),
            "reason": delivery.get("reason"),
            "channel_id": delivery.get("channel_id"),
            "to": delivery.get("to"),
            "message_ids": delivery.get("message_ids"),
            "error_hash": delivery.get("error_hash"),
            "deliveries": delivery.get("deliveries"),
        }
    )


def _classify_reason(reason: str) -> str:
    if reason == "discord_http_403":
        return "discord_permission_denied"
    return reason or "delivery_failed"


def _safe_message(value: str) -> str:
    lines = [
        re.sub(r"[ \t]+", " ", SENSITIVE_TEXT_RE.sub("[redacted]", line)).rstrip()
        for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(lines).strip()[:4000]


def _safe_subject(value: str) -> str:
    text = re.sub(r"\s+", " ", SENSITIVE_TEXT_RE.sub("[redacted]", str(value or ""))).strip()
    return text[:180] or "Rocky assistant notification"


def _fallback_email_text(message: str, discord_delivery: dict[str, Any]) -> str:
    reason = str(discord_delivery.get("reason") or "discord_delivery_failed")
    header = "\n".join(
        [
            "Fallback email from Rocky.",
            f"Primary Discord delivery failed: {reason}.",
            "Rocky is using email only because the primary Discord route is unavailable.",
            "",
        ]
    )
    return _safe_message(header + str(message or ""))


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
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch safe Rocky assistant notifications.")
    parser.add_argument("--status", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--target-date", dest="target_date")
    parser.add_argument("--idempotency-key", dest="idempotency_key")
    parser.add_argument("--channel-id", default=DEFAULT_ALERT_CHANNEL_ID, dest="channel_id")
    parser.add_argument("--fallback-email", default=DEFAULT_FALLBACK_EMAIL, dest="fallback_email")
    parser.add_argument("--config-path", default=str(DEFAULT_OPENCLAW_CONFIG_PATH), dest="config_path")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--no-agentmail-fallback", action="store_false", dest="allow_agentmail_fallback", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = {
        "status": args.status,
        "reason": args.reason,
        "target_date": args.target_date,
        "idempotency_key": args.idempotency_key,
    }
    result = dispatch_failure_notification(
        payload,
        channel_id=args.channel_id,
        fallback_email=args.fallback_email,
        config_path=args.config_path,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        dry_run=args.dry_run,
        allow_agentmail_fallback=args.allow_agentmail_fallback,
    )
    if args.json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Notification: {result.get('status')} ({result.get('reason')})")
    return 0 if result.get("status") in {"posted", "dry_run", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
