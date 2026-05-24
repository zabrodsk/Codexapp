#!/usr/bin/env python3
"""Scheduler for Rocky meeting outcome capture and follow-up automation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_notification_dispatcher import DEFAULT_ALERT_CHANNEL_ID, dispatch_failure_notification
from assistant_run_lock import acquire_run_lock, release_run_lock
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso
from investor_relationship_memory import promote_meeting_outcome_memory
from meeting_outcome_extractor import extract_meeting_outcome
from meeting_outcome_ledger import MeetingOutcomeLedger
from meeting_outcome_reader import collect_meeting_outcome_candidates
from meeting_outcome_task_applier import apply_meeting_outcome_tasks
from meeting_prep_notion_manager import update_meeting_prep_outcome
from task_focus_live_booking import book_task_focus_proposal
from task_focus_proposal_engine import build_task_focus_proposals


WORKFLOW = "meeting_outcome_scheduler"
JOB_NAME = "meeting_outcome_capture"
POLICY_VERSION = "rocky-meeting-outcome-v1"
TIMEZONE = "Europe/Prague"
DEFAULT_STATE_FILE = Path("/Users/clawdbot/.openclaw/state/meeting_outcome_scheduler.json")
DEFAULT_LOCK_TTL_SECONDS = 1200
SENSITIVE_RE = re.compile(r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def build_meeting_outcome_candidates(
    *,
    meeting_dir: str | Path | None = None,
    since_days: int = 7,
    limit: int = 20,
) -> dict[str, Any]:
    return collect_meeting_outcome_candidates(meeting_dir=meeting_dir or collect_meeting_outcome_candidates.__kwdefaults__["meeting_dir"], since_days=since_days, limit=limit)


def extract_meeting_outcome_for_key(
    *,
    meeting_key: str,
    meeting_dir: str | Path | None = None,
    since_days: int = 14,
    limit: int = 80,
    use_llm: bool = True,
    candidates_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = candidates_payload or collect_meeting_outcome_candidates(meeting_dir=meeting_dir or collect_meeting_outcome_candidates.__kwdefaults__["meeting_dir"], since_days=since_days, limit=limit)
    candidate = next((item for item in payload.get("candidates") or [] if item.get("meeting_key") == meeting_key), None)
    if not candidate:
        return {"status": "blocked", "reason": "meeting_outcome_key_not_found", "meeting_key": meeting_key, "calendar_write_attempted": False, "notion_write_attempted": False}
    return extract_meeting_outcome(candidate, use_llm=use_llm)


def apply_meeting_outcome(
    *,
    meeting_key: str,
    live: bool = False,
    apply_safe_followups: bool = False,
    meeting_dir: str | Path | None = None,
    since_days: int = 14,
    ledger_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    audit_ledger_path: str | Path | None = None,
    planning_date: str | date | None = None,
    notification_dry_run: bool = False,
    use_llm: bool = True,
    candidates_payload: dict[str, Any] | None = None,
    notion_client: Any | None = None,
    notion_config: Any | None = None,
) -> dict[str, Any]:
    extracted = extract_meeting_outcome_for_key(
        meeting_key=meeting_key,
        meeting_dir=meeting_dir,
        since_days=since_days,
        use_llm=use_llm,
        candidates_payload=candidates_payload,
    )
    if extracted.get("status") not in {"ok", "manual_review_required"}:
        return extracted
    if not live:
        task_result = apply_meeting_outcome_tasks(extracted, live=False)
        memory_result = promote_meeting_outcome_memory(extracted, live=False)
        return _redact({
            "status": "dry_run",
            "reason": "live_flag_not_supplied",
            "meeting_key": meeting_key,
            "outcome": _safe_outcome(extracted),
            "task_result": task_result,
            "memory_result": memory_result,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        })
    ledger = MeetingOutcomeLedger(ledger_db_path)
    task_result = apply_meeting_outcome_tasks(extracted, live=True, notion_client=notion_client, notion_config=notion_config)
    memory_result = promote_meeting_outcome_memory(extracted, live=True)
    notion_result = update_meeting_prep_outcome(extracted, task_result=task_result, memory_result=memory_result, live=True, client=notion_client)
    focus_result = _maybe_book_followup_focus(
        extracted,
        task_result=task_result,
        apply_safe_followups=apply_safe_followups,
        planning_date=planning_date,
        scheduler_db_path=scheduler_db_path,
        audit_ledger_path=audit_ledger_path,
    )
    ledger_row = ledger.record_outcome(
        extracted,
        status="applied" if task_result.get("status") == "ok" else "manual_review_required",
        task_refs=task_result.get("task_refs") or [],
        memory_refs=memory_result.get("memory_refs") or [],
        reason=task_result.get("reason") or extracted.get("reason"),
    )
    audit_id = _audit("meeting.outcome_applied", "completed", "meeting_outcome_applied", extracted, audit_ledger_path)
    return _redact({
        "status": "ok" if task_result.get("status") in {"ok", "dry_run"} else "manual_review_required",
        "reason": "meeting_outcome_applied",
        "meeting_key": meeting_key,
        "outcome": _safe_outcome(extracted),
        "task_result": task_result,
        "memory_result": memory_result,
        "notion_result": notion_result,
        "focus_result": focus_result,
        "ledger": ledger_row,
        "audit_id": audit_id,
        "calendar_write_attempted": bool(focus_result.get("calendar_write_attempted")),
        "notion_write_attempted": bool(task_result.get("notion_write_attempted") or notion_result.get("notion_write_attempted")),
    })


def run_meeting_outcome_scheduler(
    *,
    planning_date: str | date | None = None,
    live: bool = False,
    notify_failures: bool = False,
    notification_dry_run: bool = False,
    notification_channel_id: str | None = None,
    apply_safe_followups: bool = False,
    meeting_dir: str | Path | None = None,
    since_days: int = 7,
    limit: int = 20,
    max_meetings: int = 5,
    ledger_db_path: str | Path | None = None,
    scheduler_db_path: str | Path | None = None,
    audit_ledger_path: str | Path | None = None,
    state_file: str | Path | None = DEFAULT_STATE_FILE,
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    write_audit: bool = True,
    now_local: str | datetime | None = None,
    use_llm: bool = True,
    candidates_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _parse_now(now_local)
    planning_day = _parse_date(planning_date) if planning_date else now.date()
    bucket = (now.minute // 30) * 30
    run_key = f"meeting-outcome:{planning_day.isoformat()}:{now.hour:02d}:{bucket:02d}"
    lock = acquire_run_lock(workflow=WORKFLOW, idempotency_key=run_key, ttl_seconds=lock_ttl_seconds, db_path=scheduler_db_path, ledger_path=audit_ledger_path, write_audit=write_audit, metadata={"job_name": JOB_NAME, "live": live})
    if not lock.acquired:
        payload = {"status": "skipped_duplicate_run", "reason": lock.reason, "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False}
        _record_job_run(payload, scheduler_db_path=scheduler_db_path)
        return payload
    try:
        if planning_day.weekday() >= 5:
            payload = {"status": "skipped_weekend", "reason": "meeting_outcome_capture_runs_monday_through_friday", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False, "processed": []}
            return _finish(payload, scheduler_db_path=scheduler_db_path, audit_ledger_path=audit_ledger_path, state_file=state_file, write_audit=write_audit)
        candidates = candidates_payload or collect_meeting_outcome_candidates(meeting_dir=meeting_dir or collect_meeting_outcome_candidates.__kwdefaults__["meeting_dir"], since_days=since_days, limit=limit)
        if candidates.get("status") not in {"ok", "degraded"}:
            payload = {"status": "failed", "reason": candidates.get("reason") or "meeting_outcome_candidate_read_failed", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "calendar_write_attempted": False, "notion_write_attempted": False}
            return _finish(payload, scheduler_db_path=scheduler_db_path, audit_ledger_path=audit_ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
        ledger = MeetingOutcomeLedger(ledger_db_path)
        processed: list[dict[str, Any]] = []
        for candidate in (candidates.get("candidates") or [])[: max(1, int(max_meetings))]:
            if live:
                ledger.record_seen(candidate)
            outcome = extract_meeting_outcome(candidate, use_llm=use_llm)
            existing_outcome = ledger.get_for_outcome(outcome) if live and outcome.get("outcome_hash") else None
            if existing_outcome and existing_outcome.get("status") == "applied":
                processed.append({
                    "meeting_key": outcome.get("meeting_key"),
                    "status": "skipped_duplicate",
                    "reason": "meeting_outcome_already_applied",
                    "title": outcome.get("title"),
                    "outcome_hash": outcome.get("outcome_hash"),
                    "follow_up_count": outcome.get("follow_up_count"),
                    "decision_count": outcome.get("decision_count"),
                    "task_result": {"status": "skipped_duplicate", "reason": "meeting_outcome_already_applied", "notion_write_attempted": False},
                    "memory_result": {"status": "skipped_duplicate", "reason": "meeting_outcome_already_applied", "memory_refs": existing_outcome.get("memory_refs") or []},
                    "notion_result": {"status": "skipped_duplicate", "reason": "meeting_outcome_already_applied", "notion_write_attempted": False},
                    "focus_result": {"status": "skipped_duplicate", "reason": "meeting_outcome_already_applied", "calendar_write_attempted": False},
                })
                continue
            if live:
                ledger.record_outcome(outcome, status="extracted" if outcome.get("status") == "ok" else "manual_review_required", reason=outcome.get("reason"))
            task_result = apply_meeting_outcome_tasks(outcome, live=live) if outcome.get("status") == "ok" else {"status": "skipped", "reason": outcome.get("reason"), "task_refs": [], "notion_write_attempted": False}
            memory_result = promote_meeting_outcome_memory(outcome, live=live) if outcome.get("status") == "ok" else {"status": "skipped", "reason": outcome.get("reason"), "memory_refs": []}
            notion_result = update_meeting_prep_outcome(outcome, task_result=task_result, memory_result=memory_result, live=live) if outcome.get("status") == "ok" else {"status": "skipped", "reason": outcome.get("reason"), "notion_write_attempted": False}
            focus_result = _maybe_book_followup_focus(outcome, task_result=task_result, apply_safe_followups=apply_safe_followups and live, planning_date=planning_day, scheduler_db_path=scheduler_db_path, audit_ledger_path=audit_ledger_path)
            if live:
                ledger.record_outcome(outcome, status="applied" if task_result.get("status") == "ok" else "manual_review_required", task_refs=task_result.get("task_refs") or [], memory_refs=memory_result.get("memory_refs") or [], reason=task_result.get("reason") or outcome.get("reason"))
            processed.append({
                "meeting_key": outcome.get("meeting_key"),
                "status": outcome.get("status"),
                "reason": outcome.get("reason"),
                "title": outcome.get("title"),
                "outcome_hash": outcome.get("outcome_hash"),
                "follow_up_count": outcome.get("follow_up_count"),
                "decision_count": outcome.get("decision_count"),
                "task_result": _safe_result(task_result),
                "memory_result": _safe_result(memory_result),
                "notion_result": _safe_result(notion_result),
                "focus_result": _safe_result(focus_result),
            })
        if any(item.get("status") == "ok" for item in processed):
            status = "ok"
        elif any(item.get("status") == "skipped_duplicate" for item in processed):
            status = "skipped_duplicate_outcomes"
        else:
            status = "skipped_no_outcomes"
        payload = {
            "status": status,
            "reason": "meeting_outcome_scheduler_completed" if status == "ok" else "meeting_outcomes_already_applied" if status == "skipped_duplicate_outcomes" else "no_actionable_meeting_outcomes",
            "workflow": WORKFLOW,
            "target_date": planning_day.isoformat(),
            "run_idempotency_key": run_key,
            "candidate_count": candidates.get("candidate_count", 0),
            "processed": processed,
            "processed_count": len(processed),
            "tasks_created": sum(int(((item.get("task_result") or {}).get("created_count") or 0)) for item in processed),
            "tasks_updated": sum(int(((item.get("task_result") or {}).get("updated_count") or 0)) for item in processed),
            "memory_promoted_count": sum(len((item.get("memory_result") or {}).get("memory_refs") or []) for item in processed),
            "calendar_write_attempted": any(bool((item.get("focus_result") or {}).get("calendar_write_attempted")) for item in processed),
            "notion_write_attempted": any(bool((item.get("task_result") or {}).get("notion_write_attempted") or (item.get("notion_result") or {}).get("notion_write_attempted")) for item in processed),
            "ledger_counts_by_status": ledger.counts_by_status() if live else {},
        }
        return _finish(payload, scheduler_db_path=scheduler_db_path, audit_ledger_path=audit_ledger_path, state_file=state_file, write_audit=write_audit, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
    except Exception as exc:
        payload = {"status": "failed", "reason": "meeting_outcome_scheduler_exception", "workflow": WORKFLOW, "target_date": planning_day.isoformat(), "run_idempotency_key": run_key, "error_class": exc.__class__.__name__, "error_hash": _hash_text(str(exc)), "calendar_write_attempted": False, "notion_write_attempted": False}
        return _finish(payload, scheduler_db_path=scheduler_db_path, audit_ledger_path=audit_ledger_path, state_file=state_file, write_audit=write_audit, dead_letter=True, notify_failures=notify_failures, notification_dry_run=notification_dry_run, notification_channel_id=notification_channel_id)
    finally:
        release_run_lock(workflow=WORKFLOW, idempotency_key=run_key, db_path=scheduler_db_path, ledger_path=audit_ledger_path, write_audit=write_audit)


def list_meeting_outcome_runs(*, limit: int = 20, scheduler_db_path: str | Path | None = None, ledger_db_path: str | Path | None = None) -> dict[str, Any]:
    state = AssistantSchedulerState(scheduler_db_path)
    runs = []
    for row in state.list_job_runs(job_name=JOB_NAME, limit=max(1, int(limit))):
        summary = _parse_json(row.get("summary"))
        runs.append(_redact({"run_id": row.get("run_id"), "status": row.get("status"), "target_date": summary.get("target_date") or row.get("scheduled_for"), "processed_count": summary.get("processed_count"), "tasks_created": summary.get("tasks_created"), "memory_promoted_count": summary.get("memory_promoted_count"), "reason": summary.get("reason") or row.get("failure_class"), "created_at": row.get("created_at")}))
    ledger = MeetingOutcomeLedger(ledger_db_path)
    return {"status": "ok", "runs": runs, "count": len(runs), "ledger_recent": ledger.recent(limit=limit), "ledger_counts_by_status": ledger.counts_by_status(), "calendar_write_attempted": False, "notion_write_attempted": False}


def _maybe_book_followup_focus(outcome: dict[str, Any], *, task_result: dict[str, Any], apply_safe_followups: bool, planning_date: str | date | None, scheduler_db_path: str | Path | None, audit_ledger_path: str | Path | None) -> dict[str, Any]:
    if not apply_safe_followups:
        return {"status": "skipped", "reason": "safe_followup_booking_not_requested", "calendar_write_attempted": False}
    planning_day = _parse_date(planning_date) if planning_date else datetime.now(ZoneInfo(TIMEZONE)).date()
    if planning_day.weekday() >= 4:
        return {"status": "skipped_weekend_target", "reason": "proactive_booking_blocked_on_friday_saturday_sunday", "calendar_write_attempted": False}
    tasks = []
    for ref, task in zip(task_result.get("task_refs") or [], outcome.get("follow_up_tasks") or []):
        tasks.append({**task, "page_id": ref.get("page_id"), "calendar_block_status": "None"})
    proposals = build_task_focus_proposals(planning_date=planning_day, tasks=tasks, ledger_path=audit_ledger_path, write_audit=True)
    if proposals.get("status") != "proposal" or not proposals.get("idempotency_key"):
        return {**_safe_result(proposals), "calendar_write_attempted": False}
    return book_task_focus_proposal(idempotency_key=str(proposals["idempotency_key"]), planning_date=planning_day.isoformat(), live=True, tasks=tasks, scheduler_db_path=scheduler_db_path, ledger_path=audit_ledger_path)


def _finish(payload: dict[str, Any], *, scheduler_db_path: str | Path | None, audit_ledger_path: str | Path | None, state_file: str | Path | None, write_audit: bool, dead_letter: bool = False, notify_failures: bool = False, notification_dry_run: bool = False, notification_channel_id: str | None = None) -> dict[str, Any]:
    safe = _redact(payload)
    state = AssistantSchedulerState(scheduler_db_path)
    if dead_letter:
        safe["dead_letter"] = state.upsert_dead_letter(job_name=JOB_NAME, workflow=WORKFLOW, idempotency_key=str(safe.get("run_idempotency_key") or JOB_NAME), failure_class=str(safe.get("reason") or "meeting_outcome_failed"), safe_summary=f"Meeting outcome capture {safe.get('status')}: {safe.get('reason')}", source_refs=["meeting-outcome:scheduler"], recovery_hint="Inspect meeting-outcome-scheduler-run output, outcome ledger, Notion task health, and Obsidian status before rerunning.", error_hash=safe.get("error_hash"))
    if write_audit:
        safe["audit_id"] = _audit("scheduler.run_observed", "completed" if safe.get("status") in {"ok", "skipped_weekend", "skipped_no_outcomes", "skipped_duplicate_run", "skipped_duplicate_outcomes"} else "failed", str(safe.get("reason") or safe.get("status")), safe, audit_ledger_path)
    if notify_failures:
        safe["notification"] = dispatch_failure_notification(safe, channel_id=notification_channel_id or DEFAULT_ALERT_CHANNEL_ID, scheduler_db_path=scheduler_db_path, ledger_path=audit_ledger_path, dry_run=notification_dry_run)
    if state_file:
        _write_state(Path(state_file), safe)
    _record_job_run(safe, scheduler_db_path=scheduler_db_path)
    return safe


def _record_job_run(payload: dict[str, Any], *, scheduler_db_path: str | Path | None) -> dict[str, Any] | None:
    try:
        state = AssistantSchedulerState(scheduler_db_path)
        summary = {"kind": "meeting_outcome_run", "status": payload.get("status"), "reason": payload.get("reason"), "target_date": payload.get("target_date"), "processed_count": payload.get("processed_count", 0), "tasks_created": payload.get("tasks_created", 0), "tasks_updated": payload.get("tasks_updated", 0), "memory_promoted_count": payload.get("memory_promoted_count", 0), "calendar_write_attempted": bool(payload.get("calendar_write_attempted")), "notion_write_attempted": bool(payload.get("notion_write_attempted"))}
        return state.record_job_run(job_name=JOB_NAME, job_label="Rocky meeting outcome capture", scheduled_for=str(summary.get("target_date") or ""), finished_at=utc_now_iso(), status=str(payload.get("status") or "unknown"), idempotency_key=str(payload.get("run_idempotency_key") or JOB_NAME), launchagent_label="com.openclaw.rocky-meeting-outcome-capture", program="meeting_outcome_scheduler.py", failure_class=str(payload.get("reason")) if payload.get("status") == "failed" else None, summary=json.dumps(_redact(summary), ensure_ascii=False, sort_keys=True), error_hash=payload.get("error_hash"))
    except Exception:
        return None


def _audit(event_type: str, decision: str, reason: str, payload: dict[str, Any], ledger_path: str | Path | None) -> str | None:
    event = AssistantAuditLog(ledger_path).record_event(event_type=event_type, workflow=WORKFLOW, idempotency_key=str(payload.get("run_idempotency_key") or payload.get("outcome_hash") or payload.get("meeting_key") or JOB_NAME), policy_version=POLICY_VERSION, decision=decision, reason=reason, sources=payload.get("source_refs") or [payload.get("source_ref") or "meeting-outcome"], artifacts={"status": payload.get("status"), "meeting_key": payload.get("meeting_key"), "outcome_hash": payload.get("outcome_hash")})
    return event.audit_id


def _safe_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    return {key: outcome.get(key) for key in ["status", "reason", "meeting_key", "title", "meeting_date", "outcome_hash", "follow_up_count", "decision_count", "relationship_update_count", "warnings"]}


def _safe_result(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact({key: payload.get(key) for key in ["status", "reason", "created_count", "updated_count", "candidate_count", "memory_refs", "task_refs", "calendar_write_attempted", "notion_write_attempted", "idempotency_key"] if key in payload})


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    safe = {key: payload.get(key) for key in ["status", "reason", "target_date", "run_idempotency_key", "processed_count", "tasks_created", "tasks_updated", "memory_promoted_count", "calendar_write_attempted", "notion_write_attempted", "ledger_counts_by_status", "error_hash"]}
    safe["last_run_at"] = utc_now_iso()
    safe["last_status"] = payload.get("status")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_redact(safe), indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_RE.sub("[redacted]", value[:1000])
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Rocky meeting outcome capture.")
    parser.add_argument("--planning-date")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--notify-failures", action="store_true")
    parser.add_argument("--notification-dry-run", action="store_true")
    parser.add_argument("--apply-safe-followups", action="store_true")
    parser.add_argument("--meeting-dir")
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = run_meeting_outcome_scheduler(planning_date=args.planning_date, live=args.live, notify_failures=args.notify_failures, notification_dry_run=args.notification_dry_run, apply_safe_followups=args.apply_safe_followups, meeting_dir=args.meeting_dir, since_days=args.since_days, limit=args.limit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting outcome scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_weekend", "skipped_no_outcomes", "skipped_duplicate_run", "skipped_duplicate_outcomes"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
