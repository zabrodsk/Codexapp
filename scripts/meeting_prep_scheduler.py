#!/usr/bin/env python3
"""Scheduler for Rocky pre-meeting prep briefings."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_notification_dispatcher import DEFAULT_ALERT_CHANNEL_ID, dispatch_user_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from meeting_calendar_reader import read_upcoming_meetings
from meeting_context_enricher import enrich_meeting_context
from meeting_context_note_capture import run_meeting_context_note_capture
from meeting_context_note_ledger import MeetingContextNoteLedger
from meeting_prep_briefing_builder import build_meeting_prep_briefing
from meeting_prep_notion_manager import ensure_meeting_prep_database_schema, upsert_meeting_prep_note

WORKFLOW = "meeting_prep_scheduler"
JOB_NAME = "meeting_prep_briefing"
POLICY_VERSION = "rocky-meeting-prep-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/meeting_prep_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 600
SENSITIVE_RE = re.compile(r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def build_meeting_prep_candidates(
    *,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    note_ledger_db_path: str | Path | None = None,
    days: int = 1,
    limit: int = 20,
    include_context: bool = False,
    meetings_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    meetings = meetings_payload or read_upcoming_meetings(planning_date=planning_day, days=days, db_path=db_path, limit=limit)
    candidates: list[dict[str, Any]] = []
    for meeting in meetings.get("meetings") or []:
        context = None
        context_status = "calendar_clues_ready"
        if include_context:
            context = enrich_meeting_context(meeting, note_ledger_db_path=note_ledger_db_path)
            context_status = str(context.get("status") or "unknown")
        candidates.append(
            {
                "meeting_key": meeting.get("meeting_key"),
                "title": meeting.get("title"),
                "start_local": meeting.get("start_local"),
                "end_local": meeting.get("end_local"),
                "participant_count": meeting.get("participant_count"),
                "participant_domains": meeting.get("participant_domains"),
                "context_status": context_status,
                "context_count": (context or {}).get("context_count"),
                "confidence": (context or {}).get("confidence"),
                "source_refs": ((context or {}).get("source_refs") or [])[:10],
            }
        )
    return _redact_payload(
        {
            "status": meetings.get("status"),
            "reason": "meeting_prep_candidates_built",
            "planning_date": planning_day.isoformat(),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    )


def build_meeting_prep_for_key(
    *,
    meeting_key: str,
    planning_date: str | date | None = None,
    db_path: str | Path | None = None,
    note_ledger_db_path: str | Path | None = None,
    meetings_payload: dict[str, Any] | None = None,
    use_llm: bool = False,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    meetings = meetings_payload or read_upcoming_meetings(planning_date=planning_day, days=1, db_path=db_path, limit=100)
    meeting = next((item for item in meetings.get("meetings") or [] if item.get("meeting_key") == meeting_key), None)
    if not meeting:
        return {"status": "blocked", "reason": "meeting_key_not_found", "meeting_key": meeting_key, "calendar_write_attempted": False, "notion_write_attempted": False}
    enriched = enrich_meeting_context(meeting, note_ledger_db_path=note_ledger_db_path)
    brief = build_meeting_prep_briefing(meeting, enriched, use_llm=use_llm, llm_func=llm_func)
    return _redact_payload({"status": brief.get("status"), "reason": brief.get("reason"), "meeting": meeting, "context": enriched, "briefing": brief, "calendar_write_attempted": False, "notion_write_attempted": False})


def run_meeting_prep_scheduler(
    *,
    planning_date: str | date | None = None,
    live: bool = False,
    notify: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    db_path: str | Path | None = None,
    note_ledger_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    now_local: str | datetime | None = None,
    post_func: Any | None = None,
    meetings_payload: dict[str, Any] | None = None,
    max_meetings: int = 3,
    due_start_minutes: int = 15,
    due_end_minutes: int = 60,
    capture_context_notes: bool = True,
    notion_client: Any | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    bucket = (now.minute // 15) * 15
    run_key = f"meeting-prep:{planning_day.isoformat()}:{now.hour:02d}:{bucket:02d}"
    lock = acquire_run_lock(workflow=WORKFLOW, idempotency_key=run_key, ttl_seconds=lock_ttl_seconds, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit, metadata={"job_name": JOB_NAME, "live": bool(live), "notify": bool(notify)})
    if not lock.acquired:
        payload = {"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False}
        _record_job_run(payload, scheduler_db_path=scheduler_db_path)
        return payload
    try:
        if planning_day.weekday() >= 5:
            payload = {"status": "skipped_weekend", "reason": "meeting_prep_runs_monday_through_friday", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False, "processed": []}
            return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)
        note_capture = {"status": "skipped", "reason": "context_note_capture_disabled"}
        if capture_context_notes and live:
            note_capture = run_meeting_context_note_capture(live=True, acknowledge=True, notification_dry_run=notification_dry_run, ledger_db_path=note_ledger_db_path)
        meetings = meetings_payload or read_upcoming_meetings(planning_date=planning_day, days=1, db_path=db_path, limit=80)
        if meetings.get("status") not in {"ok", "degraded"}:
            payload = {"status": "failed", "reason": meetings.get("reason") or "calendar_meeting_read_failed", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False, "note_capture": note_capture}
            return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True)
        due = _due_meetings(meetings.get("meetings") or [], now=now, start_min=due_start_minutes, end_min=due_end_minutes)
        if not due:
            payload = {"status": "skipped_no_due_meetings", "reason": "no_meetings_in_prep_window", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False, "note_capture": note_capture, "processed": []}
            return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit)
        processed: list[dict[str, Any]] = []
        sent_count = 0
        notion_write_attempted = False
        for meeting in due[: max(1, int(max_meetings))]:
            enriched = enrich_meeting_context(meeting, note_ledger_db_path=note_ledger_db_path)
            brief = build_meeting_prep_briefing(meeting, enriched)
            if brief.get("status") == "skipped_no_context":
                processed.append({"meeting_key": meeting.get("meeting_key"), "status": "skipped_no_context", "title": meeting.get("title"), "start_local": meeting.get("start_local")})
                continue
            notion_status = {"status": "skipped", "reason": "live_flag_not_supplied"}
            notification = {"status": "skipped", "reason": "notify_disabled"}
            if live:
                schema = ensure_meeting_prep_database_schema(live=True, client=notion_client)
                if schema.get("status") in {"ok", "created"}:
                    notion_status = upsert_meeting_prep_note(meeting, brief, live=True, client=notion_client, discord_status="Dry run" if notification_dry_run else "Not sent")
                    notion_write_attempted = notion_write_attempted or bool(notion_status.get("notion_write_attempted"))
                else:
                    notion_status = schema
            if notify:
                notification = _send_discord(
                    brief.get("discord_message"),
                    channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID,
                    dry_run=notification_dry_run,
                    post_func=post_func,
                    state_file=Path(state_file) if state_file else None,
                    meeting_key=str(meeting.get("meeting_key") or ""),
                    message_hash=str(brief.get("message_sha256") or ""),
                )
                if notification.get("status") in {"posted", "dry_run"} and live and notion_status.get("status") in {"created", "updated"} and not notification_dry_run:
                    try:
                        notion_status = upsert_meeting_prep_note(meeting, brief, live=True, client=notion_client, discord_status="Sent")
                    except Exception:
                        pass
                if notification.get("status") in {"posted", "dry_run", "skipped"}:
                    sent_count += 1 if notification.get("status") in {"posted", "dry_run"} else 0
            processed.append({"meeting_key": meeting.get("meeting_key"), "title": meeting.get("title"), "start_local": meeting.get("start_local"), "status": brief.get("status"), "context_count": enriched.get("context_count"), "notion": _safe_result(notion_status), "notification": _safe_result(notification), "message_sha256": brief.get("message_sha256")})
        status = "ok" if any(item.get("status") == "ok" for item in processed) else "skipped_no_context"
        payload = {"status": status, "reason": "meeting_prep_scheduler_completed" if status == "ok" else "no_context_for_due_meetings", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "processed": processed, "processed_count": len(processed), "sent_count": sent_count, "note_capture": note_capture, "calendar_write_attempted": False, "notion_write_attempted": notion_write_attempted}
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=False)
    except Exception as exc:
        payload = {"status": "failed", "reason": "meeting_prep_scheduler_exception", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "error_class": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "calendar_write_attempted": False, "notion_write_attempted": False}
        return _finish(payload, scheduler_db_path=scheduler_db_path, ledger_path=ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=ledger_path, write_audit=write_audit)


def list_meeting_prep_runs(*, limit: int = 20, scheduler_db_path: str | Path | None = None) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    rows = state.list_job_runs(job_name=JOB_NAME, limit=max(int(limit) * 3, int(limit)))
    runs = []
    for row in rows:
        summary = _parse_json(row.get("summary"))
        runs.append(_redact_payload({"run_id": row.get("run_id"), "status": row.get("status"), "target_date": summary.get("target_date") or row.get("scheduled_for"), "processed_count": summary.get("processed_count"), "sent_count": summary.get("sent_count"), "reason": summary.get("reason") or row.get("failure_class"), "idempotency_key": row.get("idempotency_key"), "created_at": row.get("created_at")}))
        if len(runs) >= limit:
            break
    return {"status": "ok", "count": len(runs), "runs": runs, "calendar_write_attempted": False, "notion_write_attempted": False}


def _due_meetings(meetings: list[dict[str, Any]], *, now: datetime, start_min: int, end_min: int) -> list[dict[str, Any]]:
    due = []
    window_start = now + timedelta(minutes=max(0, int(start_min)))
    window_end = now + timedelta(minutes=max(int(start_min), int(end_min)))
    for meeting in meetings:
        start = _parse_meeting_time(meeting.get("start_local"), tz=now.tzinfo or ZoneInfo(TIMEZONE))
        if start and window_start <= start <= window_end:
            due.append(meeting)
    return due


def _send_discord(message: str | None, *, channel_id: str, dry_run: bool, post_func: Any | None, state_file: Path | None, meeting_key: str, message_hash: str) -> dict[str, Any]:
    safe_message = _safe_multiline(message or "Rocky meeting prep is empty.", 1800)
    if state_file and _already_sent(state_file, meeting_key=meeting_key, message_hash=message_hash):
        return {"status": "skipped", "reason": "meeting_prep_already_sent", "notification_attempted": False}
    if dry_run:
        return {"status": "dry_run", "reason": "notification_dry_run", "channel_id": channel_id, "message_preview": safe_message[:700], "message_sha256": _hash_text(safe_message), "notification_attempted": False}
    try:
        delivery = dispatch_user_notification(
            workflow=WORKFLOW,
            message=safe_message,
            subject="Rocky meeting prep briefing",
            reason="meeting_prep_briefing",
            idempotency_key=f"meeting-prep:{meeting_key}:{message_hash or _hash_text(safe_message)}",
            channel_id=channel_id,
            post_func=post_func,
        )
    except Exception as exc:
        return {"status": "failed", "reason": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "notification_attempted": True}
    if state_file and str(delivery.get("status") or "posted") in {"posted", "ok", "success"}:
        _mark_sent(state_file, meeting_key=meeting_key, message_hash=message_hash)
    return {"status": delivery.get("status") or "posted", "channel_id": channel_id, "message_sha256": _hash_text(safe_message), "notification_attempted": True, "delivery": _safe_result(delivery)}


def _finish(payload: dict[str, Any], *, scheduler_db_path: str | Path | None, ledger_path: str | Path | None, state_file: str | Path | None, write_audit: bool, dead_letter: bool = False) -> dict[str, Any]:
    safe = _redact_payload(payload)
    if dead_letter:
        state = AssistantSchedulerState(scheduler_db_path)
        safe["dead_letter"] = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), failure_class=str(safe.get("reason") or "meeting_prep_failed"), safe_summary=f"Meeting prep {safe.get('status')}: {safe.get('reason')}", source_refs=["meeting-prep:scheduler"], recovery_hint="Inspect meeting-prep-scheduler-run output, Notion health, Discord delivery, and Calendar read access before rerunning.", error_hash=safe.get("error_hash"))
    if write_audit:
        event = AssistantAuditLog(ledger_path).record_event(event_type="scheduler.run_observed", workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), policy_version=POLICY_VERSION, decision="completed" if safe.get("status") in {"ok", "skipped_weekend", "skipped_no_due_meetings", "skipped_no_context"} else "failed", reason=str(safe.get("reason") or safe.get("status")), sources=["meeting-prep:scheduler"], artifacts={"status": safe.get("status"), "target_date": safe.get("target_date"), "processed_count": safe.get("processed_count")})
        safe["audit_id"] = event.audit_id
    if state_file:
        _write_state(Path(state_file), safe)
    _record_job_run(safe, scheduler_db_path=scheduler_db_path)
    return safe


def _record_job_run(payload: dict[str, Any], *, scheduler_db_path: str | Path | None) -> dict[str, Any] | None:
    try:
        state = AssistantSchedulerState(scheduler_db_path)
        summary = {"kind": "meeting_prep_run", "status": payload.get("status"), "reason": payload.get("reason"), "target_date": payload.get("target_date"), "processed_count": payload.get("processed_count", 0), "sent_count": payload.get("sent_count", 0), "calendar_write_attempted": False, "notion_write_attempted": bool(payload.get("notion_write_attempted"))}
        return state.record_job_run(job_name=JOB_NAME, job_label="Rocky meeting prep briefing", scheduled_for=str(summary.get("target_date") or ""), finished_at=utc_now_iso(), status=str(payload.get("status") or "unknown"), idempotency_key=str(payload.get("run_idempotency_key") or JOB_NAME), launchagent_label="com.openclaw.rocky-meeting-prep-briefing", program="meeting_prep_scheduler.py", failure_class=str(payload.get("reason")) if payload.get("status") == "failed" else None, summary=json.dumps(_redact_payload(summary), ensure_ascii=False, sort_keys=True), error_hash=payload.get("error_hash"))
    except Exception:
        return None


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_json(path)
    sent = current.get("sent") or {}
    payload_state = {"last_run_at": utc_now_iso(), "last_status": payload.get("status"), "target_date": payload.get("target_date"), "reason": payload.get("reason"), "processed_count": payload.get("processed_count", 0), "sent_count": payload.get("sent_count", 0), "calendar_write_attempted": False, "notion_write_attempted": bool(payload.get("notion_write_attempted")), "error_hash": payload.get("error_hash"), "sent": sent}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_redact_payload(payload_state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _already_sent(path: Path, *, meeting_key: str, message_hash: str) -> bool:
    state = _read_json(path)
    sent = (state.get("sent") or {}).get(meeting_key) or {}
    return bool(sent.get("message_hash") == message_hash)


def _mark_sent(path: Path, *, meeting_key: str, message_hash: str) -> None:
    state = _read_json(path)
    sent = dict(state.get("sent") or {})
    sent[meeting_key] = {"message_hash": message_hash, "sent_at": utc_now_iso()}
    state["sent"] = sent
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_redact_payload(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def _parse_meeting_time(value: Any, *, tz) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except Exception:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_payload(result.get(key)) for key in ("status", "reason", "page_id", "message_ids", "channel_id", "message_sha256", "notification_attempted") if key in result}


def _safe_multiline(value: Any, limit: int = 1800) -> str:
    lines = [re.sub(r"[ \t]+", " ", SENSITIVE_RE.sub("[redacted]", line)).strip() for line in str(value or "").splitlines()]
    return "\n".join(lines).strip()[:limit]


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return SENSITIVE_RE.sub("[redacted]", text)[:limit]


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
        return _safe_text(value, 1000)
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Rocky meeting prep scheduler.")
    parser.add_argument("--planning-date", dest="planning_date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    parser.add_argument("--notification-channel-id", dest="notification_channel_id")
    parser.add_argument("--db-path", dest="db_path")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--audit-ledger", dest="audit_ledger")
    parser.add_argument("--state-file", dest="state_file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_meeting_prep_scheduler(planning_date=args.planning_date, live=args.live, notify=args.notify, notification_dry_run=args.notification_dry_run, notification_channel_id=args.notification_channel_id, db_path=args.db_path, scheduler_db_path=args.scheduler_db, ledger_path=args.audit_ledger, state_file=args.state_file)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting prep scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_weekend", "skipped_no_due_meetings", "skipped_no_context"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
