#!/usr/bin/env python3
"""Scheduler for Rocky's daily personal assistant briefing and priority arbitration."""
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
from assistant_notification_dispatcher import DEFAULT_ALERT_CHANNEL_ID, DEFAULT_OPENCLAW_CONFIG_PATH, dispatch_user_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from coding_focus_live_booking import book_coding_focus_proposal
from daily_personal_briefing_renderer import render_daily_personal_briefing
from daily_personal_signal_collector import collect_daily_personal_signals
from daily_priority_arbitrator import arbitrate_daily_priorities
from email_triage_scheduler import run_email_triage_scheduler
from task_focus_live_booking import book_task_focus_proposal

WORKFLOW = "daily_personal_briefing_scheduler"
JOB_NAME = "daily_personal_briefing"
POLICY_VERSION = "rocky-daily-personal-briefing-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/daily_personal_briefing_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 1800
DISCORD_API = "https://discord.com/api/v10"
SENSITIVE_TEXT_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_daily_personal_briefing(
    *,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    signals_payload: dict[str, Any] | None = None,
    arbitration_payload: dict[str, Any] | None = None,
    use_llm: bool = False,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    signals = signals_payload or collect_daily_personal_signals(planning_date=planning_day, db_path=db_path, scheduler_db_path=scheduler_db_path)
    arbitration = arbitration_payload or arbitrate_daily_priorities(signals, use_llm=use_llm, llm_func=llm_func)
    rendered = render_daily_personal_briefing(signals, arbitration)
    return _redact_payload({
        "status": "ok" if signals.get("status") == "ok" and arbitration.get("status") == "ok" else "degraded",
        "reason": "daily_personal_briefing_built",
        "workflow": "daily_personal_briefing",
        "planning_date": planning_day.isoformat(),
        "signals": _safe_signals(signals),
        "arbitration": arbitration,
        "rendered": rendered,
        "discord_message": rendered.get("discord_message"),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def explain_daily_priorities(
    *,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    use_llm: bool = False,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    briefing = run_daily_personal_briefing(
        planning_date=planning_date,
        db_path=db_path,
        scheduler_db_path=scheduler_db_path,
        use_llm=use_llm,
        llm_func=llm_func,
    )
    arbitration = briefing.get("arbitration") or {}
    return _redact_payload(
        {
            "status": briefing.get("status"),
            "reason": "daily_priority_explanation_built",
            "planning_date": briefing.get("planning_date"),
            "top_priority": arbitration.get("top_priority"),
            "explanations": arbitration.get("explanations") or [],
            "deferred_or_did_not_fit": arbitration.get("deferred_or_did_not_fit") or [],
            "safe_booking_actions": arbitration.get("safe_booking_actions") or [],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    )


def run_daily_personal_briefing_scheduler(
    *,
    planning_date: str | date | None = None,
    live: bool = False,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    apply_safe_bookings: bool = False,
    db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    signals_payload: dict[str, Any] | None = None,
    arbitration_payload: dict[str, Any] | None = None,
    now_local: str | datetime | None = None,
    post_func: Any | None = None,
    use_llm: bool = False,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    run_key = f"daily-personal-briefing:{planning_day.isoformat()}"
    lock = acquire_run_lock(
        workflow=WORKFLOW,
        idempotency_key=run_key,
        ttl_seconds=lock_ttl_seconds,
        db_path=scheduler_db_path,
        ledger_path=ledger_path,
        write_audit=write_audit,
        metadata={"job_name": JOB_NAME, "planning_date": planning_day.isoformat(), "live": bool(live)},
    )
    if not lock.acquired:
        payload = _redact_payload({"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "lock": lock.to_dict(), "calendar_write_attempted": False, "notion_write_attempted": False})
        _record_daily_job_run(payload, scheduler_db_path=scheduler_db_path)
        return payload
    try:
        if planning_day.weekday() >= 5:
            payload = {
                "status": "skipped_weekend_briefing",
                "reason": "daily_briefing_runs_monday_through_friday",
                "workflow": WORKFLOW,
                "target_date": planning_day.isoformat(),
                "run_idempotency_key": run_key,
                "lock": lock.to_dict(),
                "notification": {"status": "skipped", "reason": "weekend_briefing_skipped"},
                "booking_results": [],
                "calendar_write_attempted": False,
                "notion_write_attempted": False,
            }
            return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)

        briefing = run_daily_personal_briefing(
            planning_date=planning_day,
            db_path=db_path,
            scheduler_db_path=scheduler_db_path,
            signals_payload=signals_payload,
            arbitration_payload=arbitration_payload,
            use_llm=use_llm,
            llm_func=llm_func,
        )
        signals = briefing.get("signals") or _safe_signals(signals_payload or {})
        arbitration = briefing.get("arbitration") or {}
        booking_results: list[dict[str, Any]] = []
        safe_booking_mode = _safe_booking_mode(planning_day=planning_day, now=now, live=live, apply_safe_bookings=apply_safe_bookings, booking_allowed=bool((signals_payload or signals).get("booking_allowed_today", signals.get("booking_allowed_today"))))
        if safe_booking_mode == "live":
            booking_results = _apply_safe_bookings(
                arbitration.get("safe_booking_actions") or [],
                planning_day=planning_day,
                db_path=db_path,
                calendar_state_db_path=calendar_state_db_path,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
            )
        notification = _send_routed_briefing(
            briefing.get("discord_message") or ((briefing.get("rendered") or {}).get("discord_message")),
            channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID,
            dry_run=notification_dry_run,
            target_date=planning_day.isoformat(),
            idempotency_key=run_key,
            ledger_path=ledger_path,
            scheduler_db_path=scheduler_db_path,
            post_func=post_func,
        ) if notify else {"status": "skipped", "reason": "notify_disabled"}
        notification_failed = notification.get("status") == "failed"
        payload = {
            "status": "degraded" if notification_failed else "ok" if briefing.get("status") in {"ok", "degraded"} else str(briefing.get("status") or "failed"),
            "reason": "daily_personal_briefing_notification_failed" if notification_failed else "daily_personal_briefing_completed" if briefing.get("status") == "ok" else str(briefing.get("reason") or "daily_personal_briefing_degraded"),
            "workflow": WORKFLOW,
            "target_date": planning_day.isoformat(),
            "run_idempotency_key": run_key,
            "briefing": _safe_briefing(briefing),
            "arbitration": arbitration,
            "safe_booking_mode": safe_booking_mode,
            "booking_results": booking_results,
            "notification": notification,
            "calendar_write_attempted": any(bool(item.get("calendar_write_attempted")) for item in booking_results),
            "notion_write_attempted": any(bool(item.get("notion_write_attempted")) for item in booking_results),
            "created_count": sum(1 for item in booking_results if item.get("status") == "created"),
            "skipped_count": sum(1 for item in booking_results if str(item.get("status") or "").startswith("skipped")),
            "lock": lock.to_dict(),
        }
        dead_letter = payload["status"] not in {"ok", "degraded"} or notification_failed
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=dead_letter)
    except Exception as exc:
        payload = {"status": "failed", "reason": "daily_personal_briefing_exception", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "error_class": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "calendar_write_attempted": False, "notion_write_attempted": False}
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _apply_safe_bookings(actions: list[dict[str, Any]], *, planning_day: date, db_path: str | Path | None, calendar_state_db_path: str | Path | None, scheduler_db_path: str | Path | None, ledger_path: str | Path | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions[:2]:
        kind = str(action.get("action") or "")
        key = str(action.get("idempotency_key") or "")
        if not key:
            continue
        if kind == "email_triage_repair":
            result = run_email_triage_scheduler(planning_date=planning_day.isoformat(), live=True, notify_failures=False, db_path=db_path, calendar_state_db_path=calendar_state_db_path, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path)
        elif kind == "coding_focus_book":
            result = book_coding_focus_proposal(idempotency_key=key, planning_date=planning_day.isoformat(), calendar_name="Calendar", live=True, db_path=db_path, state_db_path=calendar_state_db_path, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path)
        elif kind == "task_focus_book":
            result = book_task_focus_proposal(idempotency_key=key, planning_date=planning_day.isoformat(), calendar_name="Calendar", live=True, db_path=db_path, state_db_path=calendar_state_db_path, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path)
        else:
            result = {"status": "blocked", "reason": "unknown_safe_booking_action", "calendar_write_attempted": False}
        results.append({"action": kind, "idempotency_key": key, **_safe_result(result)})
        if result.get("status") not in {"created", "skipped_duplicate", "ok", "skipped_no_attention_emails", "skipped_no_coding_focus"}:
            break
    return results


def _safe_booking_mode(*, planning_day: date, now: datetime, live: bool, apply_safe_bookings: bool, booking_allowed: bool) -> str:
    if not apply_safe_bookings:
        return "disabled"
    if not live:
        return "dry_run_no_live"
    if not booking_allowed or planning_day.weekday() >= 4:
        return "blocked_by_policy"
    if planning_day != now.date():
        return "dry_run_not_today"
    return "live"


def _send_routed_briefing(message: str | None, *, channel_id: str, dry_run: bool, target_date: str, idempotency_key: str, ledger_path: str | Path | None, scheduler_db_path: str | Path | None, post_func: Any | None = None) -> dict[str, Any]:
    safe_message = _safe_multiline_text(message or "Rocky daily brief is empty.", 1850)
    return dispatch_user_notification(
        workflow="daily_personal_briefing",
        message=safe_message,
        subject=f"Rocky daily brief - {target_date}",
        reason="daily_personal_briefing_delivery",
        target_date=target_date,
        idempotency_key=idempotency_key,
        channel_id=channel_id,
        ledger_path=ledger_path,
        scheduler_db_path=scheduler_db_path,
        dry_run=dry_run,
        post_func=post_func,
    )


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
        safe["dead_letter"] = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), failure_class=str(safe.get("reason") or "daily_personal_briefing_failed"), safe_summary=f"Daily personal briefing {safe.get('status')}: {safe.get('reason')}", source_refs=["daily-personal-briefing:scheduler"], recovery_hint="Inspect daily-personal-briefing output, lane scheduler states, and Discord delivery before rerunning.", error_hash=safe.get("error_hash"))
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(
            event_type="scheduler.run_observed",
            workflow=WORKFLOW,
            idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME),
            policy_version=POLICY_VERSION,
            decision="completed" if safe.get("status") in {"ok", "degraded", "skipped_weekend_briefing"} else "failed",
            reason=str(safe.get("reason") or safe.get("status")),
            sources=["daily-personal-briefing:scheduler"],
            artifacts={"status": safe.get("status"), "target_date": safe.get("target_date"), "booking_results": safe.get("booking_results"), "notification": safe.get("notification")},
        )
        safe["audit_id"] = event.audit_id
    if state_file:
        _write_state(Path(state_file), safe)
    _record_daily_job_run(safe, scheduler_db_path=scheduler_db_path)
    return safe


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run_at": utc_now_iso(),
        "last_status": payload.get("status"),
        "target_date": payload.get("target_date"),
        "reason": payload.get("reason"),
        "top_priority": ((payload.get("arbitration") or {}).get("top_priority") if isinstance(payload.get("arbitration"), dict) else None),
        "created_count": payload.get("created_count", 0),
        "skipped_count": payload.get("skipped_count", 0),
        "booking_results": payload.get("booking_results", []),
        "notification_status": (payload.get("notification") or {}).get("status") if isinstance(payload.get("notification"), dict) else None,
        "error_hash": payload.get("error_hash"),
    }
    path.write_text(json.dumps(_redact_payload(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_daily_job_run(payload: dict[str, Any], *, scheduler_db_path: str | Path | None) -> dict[str, Any] | None:
    try:
        state = AssistantSchedulerState(scheduler_db_path)
        briefing = payload.get("briefing") or {}
        rendered = (payload.get("rendered") or {}) if isinstance(payload.get("rendered"), dict) else {}
        summary_payload = {
            "kind": "daily_personal_briefing_run",
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "target_date": payload.get("target_date") or payload.get("planning_date"),
            "notification_status": (payload.get("notification") or {}).get("status") if isinstance(payload.get("notification"), dict) else None,
            "safe_booking_mode": payload.get("safe_booking_mode"),
            "top_priority": ((payload.get("arbitration") or {}).get("top_priority") if isinstance(payload.get("arbitration"), dict) else None),
            "created_count": payload.get("created_count", 0),
            "skipped_count": payload.get("skipped_count", 0),
            "message_sha256": briefing.get("message_sha256") or rendered.get("message_sha256"),
        }
        return state.record_job_run(
            job_name=JOB_NAME,
            job_label="Rocky daily personal briefing",
            scheduled_for=str(summary_payload.get("target_date") or ""),
            finished_at=utc_now_iso(),
            status=str(payload.get("status") or "unknown"),
            idempotency_key=str(payload.get("run_idempotency_key") or JOB_NAME),
            launchagent_label="com.openclaw.rocky-daily-personal-briefing",
            program="daily_personal_briefing_scheduler.py",
            failure_class=str(payload.get("reason")) if payload.get("status") in {"failed", "blocked"} else None,
            summary=json.dumps(_redact_payload(summary_payload), ensure_ascii=False, sort_keys=True),
            error_hash=payload.get("error_hash"),
        )
    except Exception:
        return None


def list_daily_personal_briefing_runs(*, limit: int = 20, scheduler_db_path: str | Path | None = None) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    rows = state.list_job_runs(job_name=JOB_NAME, limit=max(int(limit) * 3, int(limit)))
    runs: list[dict[str, Any]] = []
    for row in rows:
        if not str(row.get("idempotency_key") or "").startswith("daily-personal-briefing:"):
            continue
        summary = _parse_summary(row.get("summary"))
        runs.append(
            _redact_payload(
                {
                    "run_id": row.get("run_id"),
                    "status": row.get("status"),
                    "target_date": summary.get("target_date") or row.get("scheduled_for"),
                    "reason": summary.get("reason") or row.get("failure_class"),
                    "notification_status": summary.get("notification_status"),
                    "safe_booking_mode": summary.get("safe_booking_mode"),
                    "top_priority": summary.get("top_priority"),
                    "created_count": summary.get("created_count", 0),
                    "skipped_count": summary.get("skipped_count", 0),
                    "message_sha256": summary.get("message_sha256"),
                    "idempotency_key": row.get("idempotency_key"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        )
        if len(runs) >= limit:
            break
    return {"status": "ok", "count": len(runs), "runs": runs, "calendar_write_attempted": False, "notion_write_attempted": False}


def _parse_summary(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _safe_signals(signals: dict[str, Any]) -> dict[str, Any]:
    return {key: signals.get(key) for key in ("status", "planning_date", "booking_allowed_today", "booking_policy_reason", "calendar", "training", "email", "tasks", "task_focus", "coding", "command_activity", "scheduler", "dead_letters", "errors") if key in signals}


def _safe_briefing(briefing: dict[str, Any]) -> dict[str, Any]:
    rendered = briefing.get("rendered") or {}
    return {"status": briefing.get("status"), "reason": briefing.get("reason"), "planning_date": briefing.get("planning_date"), "message_sha256": rendered.get("message_sha256"), "message_chars": rendered.get("message_chars"), "discord_message": rendered.get("discord_message") or briefing.get("discord_message")}


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "reason", "calendar_write_attempted", "calendar_event_created", "calendar_event_deleted", "notion_write_attempted", "audit_id", "message_ids", "channel_id", "error_hash")
    return {key: _redact_payload(result.get(key)) for key in keys if key in result}


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


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_TEXT_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_multiline_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    safe_lines = []
    for line in text.split("\n"):
        safe_lines.append(re.sub(r"[ \t]+", " ", SENSITIVE_TEXT_RE.sub("[redacted]", line)).strip())
    safe = "\n".join(safe_lines).strip()
    return safe[:limit]


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"discord_message", "message_preview"} and isinstance(item, str):
                safe[key_text] = _safe_multiline_text(item, 2000 if key_text == "discord_message" else 800)
            else:
                safe[key_text] = _redact_payload(item)
        return safe
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 800)
    return value


def _hash_text(value: Any) -> str:
    safe = SENSITIVE_TEXT_RE.sub("[redacted]", str(value or ""))
    return hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Rocky daily personal briefing scheduler.")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--apply-safe-bookings", action="store_true", dest="apply_safe_bookings")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--scheduler-db", dest="scheduler_db_path")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", dest="state_file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_daily_personal_briefing_scheduler(
        planning_date=args.planning_date,
        live=args.live,
        notify=args.notify,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        apply_safe_bookings=args.apply_safe_bookings,
        db_path=args.db_path,
        scheduler_db_path=args.scheduler_db_path,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        write_audit=args.write_audit,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json_output else None))
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_weekend_briefing", "skipped_duplicate_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
