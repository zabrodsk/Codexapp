#!/usr/bin/env python3
"""Scheduler for Rocky's weekly personal review."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_notification_dispatcher import DEFAULT_ALERT_CHANNEL_ID, DEFAULT_OPENCLAW_CONFIG_PATH
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from weekly_personal_signal_collector import collect_weekly_personal_signals
from weekly_priority_planner import plan_weekly_priorities
from weekly_personal_review_renderer import render_weekly_personal_review

WORKFLOW = "weekly_personal_review_scheduler"
JOB_NAME = "weekly_personal_review"
POLICY_VERSION = "rocky-weekly-personal-review-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/weekly_personal_review_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 1800
DISCORD_API = "https://discord.com/api/v10"
SENSITIVE_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def build_weekly_personal_review(*, planning_date: str | date | None = None, db_path: str | Path | None = None, scheduler_db_path: str | Path | None = None, signals_payload: dict[str, Any] | None = None, plan_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    signals = signals_payload or collect_weekly_personal_signals(planning_date=planning_day, db_path=db_path, scheduler_db_path=scheduler_db_path)
    plan = plan_payload or plan_weekly_priorities(signals)
    rendered = render_weekly_personal_review(signals, plan)
    return _redact_payload({"status": "ok" if signals.get("status") == "ok" and plan.get("status") == "ok" else "degraded", "reason": "weekly_personal_review_built", "workflow": "weekly_personal_review", "planning_date": planning_day.isoformat(), "week_label": signals.get("week_label"), "signals": _safe_signals(signals), "plan": plan, "rendered": rendered, "discord_message": rendered.get("discord_message"), "calendar_write_attempted": False, "notion_write_attempted": False})


def run_weekly_personal_review_scheduler(*, planning_date: str | date | None = None, live: bool = False, notify: bool = False, notification_dry_run: bool = False, notification_channel_id: str | None = None, db_path: str | Path | None = None, scheduler_db_path: str | Path | None = None, ledger_path: str | Path | None = None, state_file: str | Path | None = DEFAULT_STATE_FILE, lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS, write_audit: bool = True, signals_payload: dict[str, Any] | None = None, plan_payload: dict[str, Any] | None = None, now_local: str | datetime | None = None, post_func: Any | None = None) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    week_label = _iso_week_label(planning_day)
    run_key = f"weekly-personal-review:{week_label}"
    lock = acquire_run_lock(workflow=WORKFLOW, idempotency_key=run_key, ttl_seconds=lock_ttl_seconds, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit, metadata={"job_name": JOB_NAME, "planning_date": planning_day.isoformat(), "live": bool(live)})
    if not lock.acquired:
        payload = _redact_payload({"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "target_week": week_label, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "lock": lock.to_dict(), "calendar_write_attempted": False, "notion_write_attempted": False})
        _record_weekly_job_run(payload, scheduler_db_path=scheduler_db_path)
        return payload
    try:
        if planning_day.weekday() != 0:
            payload = {"status": "skipped_not_weekly_review_day", "reason": "weekly_review_runs_on_monday", "workflow": WORKFLOW, "target_week": week_label, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "lock": lock.to_dict(), "notification": {"status": "skipped", "reason": "not_monday"}, "calendar_write_attempted": False, "notion_write_attempted": False}
            return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)
        review = build_weekly_personal_review(planning_date=planning_day, db_path=db_path, scheduler_db_path=scheduler_db_path, signals_payload=signals_payload, plan_payload=plan_payload)
        notification = _send_discord(review.get("discord_message") or ((review.get("rendered") or {}).get("discord_message")), channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID, dry_run=notification_dry_run, post_func=post_func) if notify else {"status": "skipped", "reason": "notify_disabled"}
        failed_notification = notification.get("status") == "failed"
        payload = {"status": "degraded" if failed_notification else "ok" if review.get("status") in {"ok", "degraded"} else str(review.get("status") or "failed"), "reason": "weekly_personal_review_notification_failed" if failed_notification else "weekly_personal_review_completed" if review.get("status") == "ok" else str(review.get("reason") or "weekly_personal_review_degraded"), "workflow": WORKFLOW, "target_week": week_label, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "review": _safe_review(review), "plan": review.get("plan") or {}, "notification": notification, "calendar_write_attempted": False, "notion_write_attempted": False, "created_count": 0, "skipped_count": 0, "lock": lock.to_dict()}
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=failed_notification)
    except Exception as exc:
        payload = {"status": "failed", "reason": "weekly_personal_review_exception", "workflow": WORKFLOW, "target_week": week_label, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "error_class": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "calendar_write_attempted": False, "notion_write_attempted": False}
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def list_weekly_personal_review_runs(*, limit: int = 20, scheduler_db_path: str | Path | None = None) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    rows = state.list_job_runs(job_name=JOB_NAME, limit=max(int(limit) * 3, int(limit)))
    runs = []
    for row in rows:
        if not str(row.get("idempotency_key") or "").startswith("weekly-personal-review:"):
            continue
        summary = _parse_summary(row.get("summary"))
        runs.append(_redact_payload({"run_id": row.get("run_id"), "status": row.get("status"), "target_week": summary.get("target_week"), "target_date": summary.get("target_date") or row.get("scheduled_for"), "reason": summary.get("reason") or row.get("failure_class"), "notification_status": summary.get("notification_status"), "message_sha256": summary.get("message_sha256"), "idempotency_key": row.get("idempotency_key"), "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}))
        if len(runs) >= limit:
            break
    return {"status": "ok", "count": len(runs), "runs": runs, "calendar_write_attempted": False, "notion_write_attempted": False}


def _send_discord(message: str | None, *, channel_id: str, dry_run: bool, post_func: Any | None = None) -> dict[str, Any]:
    safe_message = _safe_multiline(message or "Rocky weekly review is empty.", 1900)
    if dry_run:
        return {"status": "dry_run", "reason": "notification_dry_run", "channel_id": channel_id, "message_preview": safe_message[:700], "message_sha256": _hash_text(safe_message), "notification_attempted": False}
    try:
        token = _load_discord_token(DEFAULT_OPENCLAW_CONFIG_PATH)
        delivery = (post_func or _post_to_discord)(token=token, channel_id=channel_id, content=safe_message)
    except Exception as exc:
        return {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "notification_attempted": True}
    return {"status": delivery.get("status") or "posted", "channel_id": channel_id, "message_sha256": _hash_text(safe_message), "notification_attempted": True, "delivery": _safe_result(delivery)}


def _post_to_discord(*, token: str, channel_id: str, content: str) -> dict[str, Any]:
    req = request.Request(f"{DISCORD_API}/channels/{channel_id}/messages", data=json.dumps({"content": content}).encode("utf-8"), headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {"status": "posted", "channel_id": channel_id, "message_ids": [body.get("id")]}
    except error.HTTPError as exc:
        return {"status": "failed", "reason": f"discord_http_{exc.code}", "error_hash": _hash_text(str(exc))}


def _load_discord_token(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = (((payload.get("channels") or {}).get("discord") or {}).get("token") or "").strip()
    if not token:
        raise RuntimeError("discord_token_missing")
    return token


def _finish(payload: dict[str, Any], *, scheduler_db_path: str | Path | None, ledger_path: str | Path | None, state_file: str | Path | None, write_audit: bool, dead_letter: bool = False) -> dict[str, Any]:
    safe = _redact_payload(payload)
    if dead_letter:
        state = AssistantSchedulerState(scheduler_db_path)
        safe["dead_letter"] = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), failure_class=str(safe.get("reason") or "weekly_personal_review_failed"), safe_summary=f"Weekly personal review {safe.get('status')}: {safe.get('reason')}", source_refs=["weekly-personal-review:scheduler"], recovery_hint="Inspect weekly-personal-review output, scheduler state, and Discord delivery before rerunning.", error_hash=safe.get("error_hash"))
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(event_type="scheduler.run_observed", workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), policy_version=POLICY_VERSION, decision="completed" if safe.get("status") in {"ok", "degraded", "skipped_not_weekly_review_day"} else "failed", reason=str(safe.get("reason") or safe.get("status")), sources=["weekly-personal-review:scheduler"], artifacts={"status": safe.get("status"), "target_week": safe.get("target_week"), "notification": safe.get("notification")})
        safe["audit_id"] = event.audit_id
    if state_file:
        _write_state(Path(state_file), safe)
    _record_weekly_job_run(safe, scheduler_db_path=scheduler_db_path)
    return safe


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"last_run_at": utc_now_iso(), "last_status": payload.get("status"), "target_week": payload.get("target_week"), "target_date": payload.get("target_date"), "reason": payload.get("reason"), "notification_status": (payload.get("notification") or {}).get("status") if isinstance(payload.get("notification"), dict) else None, "message_sha256": ((payload.get("review") or {}).get("message_sha256") if isinstance(payload.get("review"), dict) else None), "calendar_write_attempted": bool(payload.get("calendar_write_attempted")), "notion_write_attempted": bool(payload.get("notion_write_attempted")), "error_hash": payload.get("error_hash")}
    path.write_text(json.dumps(_redact_payload(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_weekly_job_run(payload: dict[str, Any], *, scheduler_db_path: str | Path | None) -> dict[str, Any] | None:
    try:
        state = AssistantSchedulerState(scheduler_db_path)
        review = payload.get("review") or {}
        summary = {"kind": "weekly_personal_review_run", "status": payload.get("status"), "reason": payload.get("reason"), "target_week": payload.get("target_week"), "target_date": payload.get("target_date"), "notification_status": (payload.get("notification") or {}).get("status") if isinstance(payload.get("notification"), dict) else None, "message_sha256": review.get("message_sha256"), "calendar_write_attempted": bool(payload.get("calendar_write_attempted")), "notion_write_attempted": bool(payload.get("notion_write_attempted"))}
        return state.record_job_run(job_name=JOB_NAME, job_label="Rocky weekly personal review", scheduled_for=str(summary.get("target_date") or ""), finished_at=utc_now_iso(), status=str(payload.get("status") or "unknown"), idempotency_key=str(payload.get("run_idempotency_key") or JOB_NAME), launchagent_label="com.openclaw.rocky-weekly-personal-review", program="weekly_personal_review_scheduler.py", failure_class=str(payload.get("reason")) if payload.get("status") in {"failed", "blocked"} else None, summary=json.dumps(_redact_payload(summary), ensure_ascii=False, sort_keys=True), error_hash=payload.get("error_hash"))
    except Exception:
        return None


def _safe_review(review: dict[str, Any]) -> dict[str, Any]:
    rendered = review.get("rendered") or {}
    return {"status": review.get("status"), "reason": review.get("reason"), "week_label": review.get("week_label"), "message_sha256": rendered.get("message_sha256"), "message_chars": rendered.get("message_chars"), "discord_message": rendered.get("discord_message") or review.get("discord_message")}


def _safe_signals(signals: dict[str, Any]) -> dict[str, Any]:
    return {key: signals.get(key) for key in ("status", "planning_date", "week_label", "calendar", "training", "email", "tasks", "coding", "command_activity", "learning", "scheduler", "dead_letters", "calendar_hygiene", "errors") if key in signals}


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_payload(result.get(key)) for key in ("status", "reason", "message_ids", "channel_id", "error_hash") if key in result}


def _parse_summary(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}")); return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_now(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(TIMEZONE)) if value.tzinfo else value.replace(tzinfo=ZoneInfo(TIMEZONE))
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo(TIMEZONE))
    return datetime.now(ZoneInfo(TIMEZONE))


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar(); return f"{year}-W{week:02d}"


def _safe_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split()); text = SENSITIVE_RE.sub("[redacted]", text); return text[:limit]


def _safe_multiline(value: Any, limit: int = 1000) -> str:
    lines = [re.sub(r"[ \t]+", " ", SENSITIVE_RE.sub("[redacted]", line)).strip() for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()[:limit]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if str(key) in {"discord_message", "message_preview"} and isinstance(item, str):
                safe[str(key)] = _safe_multiline(item, 2000 if str(key) == "discord_message" else 900)
            else:
                safe[str(key)] = _redact_payload(item)
        return safe
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 900)
    return value


def _hash_text(value: Any) -> str:
    safe = SENSITIVE_RE.sub("[redacted]", str(value or "")); return hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Rocky weekly personal review scheduler.")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--scheduler-db", dest="scheduler_db_path")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", dest="state_file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--lock-ttl-seconds", type=int, default=DEFAULT_LOCK_TTL_SECONDS, dest="lock_ttl_seconds")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_weekly_personal_review_scheduler(planning_date=args.planning_date, live=args.live, notify=args.notify, notification_dry_run=args.notification_dry_run, notification_channel_id=args.notification_channel_id, db_path=args.db_path, scheduler_db_path=args.scheduler_db_path, ledger_path=args.ledger_path, state_file=args.state_file, lock_ttl_seconds=args.lock_ttl_seconds, write_audit=args.write_audit)
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_not_weekly_review_day", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
