#!/usr/bin/env python3
"""Automatic noon coding briefing and focus booking scheduler for Rocky."""
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

from assistant_calendar_status import calendar_write_health
from assistant_notification_dispatcher import DEFAULT_ALERT_CHANNEL_ID, DEFAULT_OPENCLAW_CONFIG_PATH, dispatch_failure_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from coding_focus_live_booking import book_coding_focus_proposal
from coding_focus_proposal_engine import build_coding_focus_proposals
from coding_work_briefing_builder import build_coding_work_briefing


WORKFLOW = "coding_work_scheduler"
JOB_NAME = "coding_work_briefing"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 1800
SAFE_SUCCESS_STATUSES = {"ok", "skipped_weekend_target", "skipped_no_coding_focus", "skipped_duplicate_run"}
ATTENTION_STATUSES = {"blocked", "failed", "manual_review_required"}
SENSITIVE_TEXT_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def run_coding_work_scheduler(
    *,
    planning_date: str | date | None = None,
    live: bool = False,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    laptop_manifest_path: str | Path | None = None,
    max_blocks: int = 2,
    use_memory: bool = True,
    db_path: str | Path | None = None,
    calendar_state_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    signals_payload: dict[str, Any] | None = None,
    work_items: list[dict[str, Any]] | None = None,
    existing_events: list[dict[str, Any]] | None = None,
    llm_func: Any | None = None,
    post_func: Any | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    run_key = f"coding-work:{planning_day.isoformat()}"
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
        return _redact_payload({"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "run_idempotency_key": run_key, "lock": lock.to_dict(), "calendar_write_attempted": False})
    try:
        payload = _run_inner(
            planning_day=planning_day,
            live=live,
            notify=notify,
            notification_dry_run=notification_dry_run,
            notification_channel_id=notification_channel_id,
            laptop_manifest_path=laptop_manifest_path,
            max_blocks=max_blocks,
            use_memory=use_memory,
            db_path=db_path,
            calendar_state_db_path=calendar_state_db_path,
            scheduler_db_path=scheduler_db_path,
            ledger_path=ledger_path,
            state_file=state_file,
            signals_payload=signals_payload,
            work_items=work_items,
            existing_events=existing_events,
            llm_func=llm_func,
            post_func=post_func,
            run_key=run_key,
        )
        return payload
    except Exception as exc:
        payload = {"status": "failed", "reason": "coding_work_scheduler_exception", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "error_class": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "calendar_write_attempted": False}
        return _finish(payload, scheduler_db_path=scheduler_db_path, state_file=state_file, notify=notify, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id, post_func=post_func, dead_letter=True)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def _run_inner(
    *,
    planning_day: date,
    live: bool,
    notify: bool,
    notification_dry_run: bool,
    notification_channel_id: str | None,
    laptop_manifest_path: str | Path | None,
    max_blocks: int,
    use_memory: bool,
    db_path: str | Path | None,
    calendar_state_db_path: str | Path | None,
    scheduler_db_path: str | Path | None,
    ledger_path: str | Path | None,
    state_file: str | Path | None,
    signals_payload: dict[str, Any] | None,
    work_items: list[dict[str, Any]] | None,
    existing_events: list[dict[str, Any]] | None,
    llm_func: Any | None,
    post_func: Any | None,
    run_key: str,
) -> dict[str, Any]:
    if planning_day.weekday() >= 4:
        return _finish({"status": "skipped_weekend_target", "reason": "proactive_booking_blocked_on_friday_saturday_sunday", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False}, scheduler_db_path=scheduler_db_path, state_file=state_file, notify=False)
    if work_items is not None:
        briefing = {
            "status": "ok" if work_items else "empty",
            "workflow": "coding_work_briefing",
            "planning_date": planning_day.isoformat(),
            "timezone": TIMEZONE,
            "signal_status": "fixture",
            "llm": {"status": "skipped", "reason": "fixture_work_items"},
            "work_item_count": len(work_items),
            "selected_count": len(work_items),
            "work_items": work_items,
            "selected_focus_items": work_items,
            "briefing": "Rocky coding briefing fixture.",
            "calendar_write_attempted": False,
        }
    else:
        briefing = build_coding_work_briefing(planning_date=planning_day, signals_payload=signals_payload, laptop_manifest_path=laptop_manifest_path, tasks=[], use_llm=True, use_memory=use_memory, llm_func=llm_func)
    proposals = build_coding_focus_proposals(planning_date=planning_day, briefing_payload=briefing, work_items=briefing.get("selected_focus_items"), db_path=db_path, ledger_path=ledger_path, write_audit=True, existing_events=existing_events, max_blocks=max_blocks)
    bookings: list[dict[str, Any]] = []
    if live and proposals.get("status") == "proposal":
        health = calendar_write_health(db_path=db_path, ledger_path=ledger_path, write_audit=False)
        if health.get("status") != "ok":
            return _finish({"status": "blocked", "reason": "calendar_write_health_blocked", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "briefing": _safe_briefing(briefing), "proposal_count": len(proposals.get("proposals") or []), "calendar_write_attempted": False}, scheduler_db_path=scheduler_db_path, state_file=state_file, notify=notify, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id, post_func=post_func, dead_letter=True)
        for proposal in (proposals.get("proposals") or [])[: max(1, int(max_blocks))]:
            if proposal.get("status") != "proposal" or not proposal.get("idempotency_key"):
                continue
            selected = proposal.get("selected_work_item") or {}
            result = book_coding_focus_proposal(
                idempotency_key=str(proposal["idempotency_key"]),
                planning_date=planning_day.isoformat(),
                calendar_name="Calendar",
                live=True,
                work_items=[selected],
                db_path=db_path,
                state_db_path=calendar_state_db_path,
                scheduler_db_path=scheduler_db_path,
                ledger_path=ledger_path,
            )
            bookings.append(result)
            if result.get("status") not in {"created", "skipped_duplicate"}:
                return _finish({"status": "blocked", "reason": str(result.get("reason") or "coding_focus_booking_failed"), "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "briefing": _safe_briefing(briefing), "proposals": _safe_proposals(proposals), "bookings": bookings, "calendar_write_attempted": bool(result.get("calendar_write_attempted"))}, scheduler_db_path=scheduler_db_path, state_file=state_file, notify=notify, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id, post_func=post_func, dead_letter=True)
    notification = _send_briefing(briefing, channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID, dry_run=notification_dry_run, post_func=post_func) if notify and briefing.get("status") == "ok" else {"status": "skipped", "reason": "notify_disabled_or_empty"}
    status = "ok" if proposals.get("status") in {"proposal", "skipped_no_coding_focus"} else str(proposals.get("status") or "blocked")
    return _finish({"status": status, "reason": proposals.get("reason"), "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "briefing": _safe_briefing(briefing), "proposals": _safe_proposals(proposals), "bookings": bookings, "notification": notification, "calendar_write_attempted": any(bool(item.get("calendar_write_attempted")) for item in bookings), "created_count": sum(1 for item in bookings if item.get("status") == "created"), "skipped_count": sum(1 for item in bookings if item.get("status") == "skipped_duplicate")}, scheduler_db_path=scheduler_db_path, state_file=state_file, notify=notify and status in ATTENTION_STATUSES, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id, post_func=post_func, dead_letter=status in ATTENTION_STATUSES)


def _send_briefing(briefing: dict[str, Any], *, channel_id: str, dry_run: bool, post_func: Any | None = None) -> dict[str, Any]:
    message = _redact_text(str(briefing.get("briefing") or "Rocky coding briefing is empty."))[:1800]
    if dry_run:
        return {"status": "dry_run", "reason": "notification_dry_run", "channel_id": channel_id, "message_sha256": _hash_text(message), "notification_attempted": False, "message_preview": message[:500]}
    try:
        token = _load_discord_token(DEFAULT_OPENCLAW_CONFIG_PATH)
        poster = post_func or _post_to_discord
        delivery = poster(token=token, channel_id=channel_id, content=message)
    except Exception as exc:
        return {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "notification_attempted": True}
    return {"status": delivery.get("status") or "posted", "channel_id": channel_id, "message_sha256": _hash_text(message), "notification_attempted": True}


def _load_discord_token(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = (((payload.get("channels") or {}).get("discord") or {}).get("token") or "").strip()
    if not token:
        raise RuntimeError("discord_token_missing")
    return token


def _post_to_discord(*, token: str, channel_id: str, content: str) -> dict[str, Any]:
    req = request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
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


def _finish(payload: dict[str, Any], *, scheduler_db_path: str | Path | None, state_file: str | Path | None, notify: bool = False, notification_dry_run: bool = False, notification_channel_id: str | None = None, post_func: Any | None = None, dead_letter: bool = False) -> dict[str, Any]:
    safe = _redact_payload(payload)
    if dead_letter:
        state = AssistantSchedulerState(scheduler_db_path)
        safe["dead_letter"] = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or safe.get("target_date") or JOB_NAME), failure_class=str(safe.get("reason") or "coding_work_attention_needed"), safe_summary=f"Coding work scheduler {safe.get('status')}: {safe.get('reason')}", source_refs=["coding-work:scheduler"], recovery_hint="Inspect coding-work-briefing and coding-focus-proposals before rerunning.", error_hash=safe.get("error_hash"))
    if notify and safe.get("status") in ATTENTION_STATUSES:
        safe["failure_notification"] = dispatch_failure_notification(safe, channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID, scheduler_db_path=scheduler_db_path, dry_run=notification_dry_run, post_func=post_func)
    if state_file:
        _write_state(Path(state_file), safe)
    return safe


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run_at": utc_now_iso(),
        "last_status": payload.get("status"),
        "target_date": payload.get("target_date"),
        "reason": payload.get("reason"),
        "created_count": payload.get("created_count", 0),
        "skipped_count": payload.get("skipped_count", 0),
        "work_item_count": ((payload.get("briefing") or {}).get("work_item_count") if isinstance(payload.get("briefing"), dict) else None),
        "memory": ((payload.get("briefing") or {}).get("memory") if isinstance(payload.get("briefing"), dict) else None),
        "error_hash": payload.get("error_hash"),
    }
    path.write_text(json.dumps(_redact_payload(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_briefing(briefing: dict[str, Any]) -> dict[str, Any]:
    return {"status": briefing.get("status"), "work_item_count": briefing.get("work_item_count"), "selected_count": briefing.get("selected_count"), "llm": briefing.get("llm"), "memory": briefing.get("memory"), "repo_visibility": briefing.get("repo_visibility"), "briefing": briefing.get("briefing"), "selected_focus_items": briefing.get("selected_focus_items")}


def _safe_proposals(proposals: dict[str, Any]) -> dict[str, Any]:
    return {"status": proposals.get("status"), "reason": proposals.get("reason"), "proposal_count": len(proposals.get("proposals") or []), "idempotency_keys": proposals.get("idempotency_keys"), "proposals": proposals.get("proposals")}


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    return SENSITIVE_TEXT_RE.sub("[redacted]", str(value or ""))


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Rocky coding work briefing and focus scheduler.")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--laptop-manifest-path", dest="laptop_manifest_path")
    parser.add_argument("--max-blocks", type=int, default=2, dest="max_blocks")
    parser.add_argument("--no-memory", action="store_false", dest="use_memory", default=True)
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), dest="state_file")
    parser.add_argument("--lock-ttl-seconds", type=int, default=DEFAULT_LOCK_TTL_SECONDS, dest="lock_ttl_seconds")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_coding_work_scheduler(planning_date=args.planning_date, live=args.live, notify=args.notify, notification_dry_run=args.notification_dry_run, notification_channel_id=args.notification_channel_id, laptop_manifest_path=args.laptop_manifest_path, max_blocks=args.max_blocks, use_memory=args.use_memory, db_path=args.db_path, calendar_state_db_path=args.calendar_state_db, scheduler_db_path=args.scheduler_db, ledger_path=args.ledger_path, state_file=args.state_file, lock_ttl_seconds=args.lock_ttl_seconds, write_audit=args.write_audit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding work scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in SAFE_SUCCESS_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
