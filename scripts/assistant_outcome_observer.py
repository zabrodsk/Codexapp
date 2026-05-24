#!/usr/bin/env python3
"""Observe sanitized outcomes from existing Rocky assistant lanes."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog
from assistant_calendar_state import AssistantCalendarState
from assistant_learning_store import AssistantLearningStore, DEFAULT_LEARNING_DB_PATH, sanitize_payload, stable_id
from assistant_scheduler_state import AssistantSchedulerState

POLICY_VERSION = "rocky-outcome-learning-v1"
WORKFLOW = "assistant_outcome_observer"


def collect_outcomes(*, since_days: int = 7, live: bool = False, learning_db_path: str | Path | None = None, scheduler_db_path: str | Path | None = None, calendar_state_db_path: str | Path | None = None, ledger_path: str | Path | None = None, write_audit: bool = True, job_runs: list[dict[str, Any]] | None = None, calendar_blocks: list[dict[str, Any]] | None = None, dead_letters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(since_days)))
    observations: list[dict[str, Any]] = []
    if calendar_blocks is None:
        try:
            calendar_blocks = AssistantCalendarState(calendar_state_db_path).list_blocks(limit=200)
        except Exception as exc:
            calendar_blocks = []
            observations.append(_system_observation("calendar_state_read_failed", str(exc)))
    observations.extend(_calendar_block_outcomes(calendar_blocks or [], cutoff=cutoff))
    if job_runs is None:
        try:
            job_runs = AssistantSchedulerState(scheduler_db_path).list_job_runs(limit=500)
        except Exception as exc:
            job_runs = []
            observations.append(_system_observation("scheduler_state_read_failed", str(exc)))
    observations.extend(_job_run_outcomes(job_runs or [], cutoff=cutoff))
    if dead_letters is None:
        try:
            dead_letters = AssistantSchedulerState(scheduler_db_path).list_dead_letters(status="open", limit=100)
        except Exception:
            dead_letters = []
    observations.extend(_dead_letter_outcomes(dead_letters or []))
    safe_observations = [sanitize_payload(obs) for obs in observations]
    written: list[dict[str, Any]] = []
    if live:
        store = AssistantLearningStore(learning_db_path)
        for obs in safe_observations:
            written.append(store.record_outcome(obs))
    audit_id = None
    if write_audit and live:
        event = AssistantAuditLog(ledger_path).record_event(event_type="assistant.outcome_observed", workflow=WORKFLOW, idempotency_key=f"assistant-outcomes:{datetime.now(timezone.utc).date().isoformat()}:{since_days}", policy_version=POLICY_VERSION, decision="completed", reason="assistant_outcomes_collected", sources=["assistant_scheduler_state", "assistant_calendar_state"], artifacts={"outcome_count": len(safe_observations), "written_count": len(written)})
        audit_id = event.audit_id
    return sanitize_payload({"status": "ok", "reason": "assistant_outcomes_collected" if live else "assistant_outcomes_preview", "live": bool(live), "since_days": int(since_days), "outcome_count": len(safe_observations), "written_count": len(written), "outcomes": written if live else safe_observations, "audit_id": audit_id, "calendar_write_attempted": False, "notion_write_attempted": False})


def _calendar_block_outcomes(blocks: list[dict[str, Any]], *, cutoff: datetime) -> list[dict[str, Any]]:
    outcomes = []
    for row in blocks:
        updated = _parse_dt(row.get("updated_at") or row.get("created_at"))
        if updated and updated < cutoff:
            continue
        title = str(row.get("title") or "")
        lane = _lane_from_title(title, row.get("idempotency_key"))
        start = _parse_dt(row.get("start"))
        end = _parse_dt(row.get("end"))
        duration = int((end - start).total_seconds() // 60) if start and end else None
        source_ref = f"calendar-state:{row.get('idempotency_key')}"
        outcomes.append({"observation_id": stable_id({"source_ref": source_ref, "status": row.get("status"), "updated_at": row.get("updated_at")}, prefix="outcome"), "observed_at": row.get("updated_at") or row.get("created_at") or datetime.now(timezone.utc).isoformat(), "lane": lane, "outcome_type": "calendar_block_lifecycle", "source_ref": source_ref, "idempotency_key": row.get("idempotency_key"), "booked_minutes": duration, "actual_minutes": duration if row.get("status") == "active" else None, "outcome_status": str(row.get("status") or "unknown"), "confidence": 0.65 if duration else 0.4, "evidence": {"title_hash": stable_id(title, prefix="title"), "status": row.get("status")}, "source_refs": [source_ref], "safe_summary": f"{lane} calendar block is {row.get('status')}"})
    return outcomes


def _job_run_outcomes(rows: list[dict[str, Any]], *, cutoff: datetime) -> list[dict[str, Any]]:
    outcomes = []
    for row in rows:
        created = _parse_dt(row.get("created_at") or row.get("started_at"))
        if created and created < cutoff:
            continue
        job = str(row.get("job_name") or "unknown")
        lane = _lane_from_job(job)
        source_ref = f"job-run:{row.get('run_id') or row.get('idempotency_key')}"
        outcomes.append({"observation_id": stable_id({"source_ref": source_ref, "status": row.get("status")}, prefix="outcome"), "observed_at": row.get("created_at") or row.get("started_at") or datetime.now(timezone.utc).isoformat(), "lane": lane, "outcome_type": "scheduler_run", "source_ref": source_ref, "idempotency_key": row.get("idempotency_key"), "outcome_status": row.get("status") or "unknown", "confidence": 0.75, "evidence": {"job_name": job, "failure_class": row.get("failure_class"), "summary_hash": stable_id(row.get("summary") or "", prefix="summary")}, "source_refs": [source_ref], "safe_summary": f"{job} scheduler run {row.get('status')}"})
    return outcomes


def _dead_letter_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcomes = []
    for row in rows:
        source_ref = f"dead-letter:{row.get('dead_letter_id')}"
        outcomes.append({"observation_id": stable_id({"source_ref": source_ref, "attempts": row.get("attempts")}, prefix="outcome"), "observed_at": row.get("last_failed_at") or row.get("created_at") or datetime.now(timezone.utc).isoformat(), "lane": _lane_from_job(row.get("job_name")), "outcome_type": "dead_letter_open", "source_ref": source_ref, "idempotency_key": row.get("idempotency_key"), "outcome_status": "attention_needed", "confidence": 0.9, "evidence": {"failure_class": row.get("failure_class"), "attempts": row.get("attempts"), "error_hash": row.get("error_hash")}, "source_refs": [source_ref], "safe_summary": f"Open dead letter for {row.get('job_name')}: {row.get('failure_class')}"})
    return outcomes


def _system_observation(reason: str, error: str) -> dict[str, Any]:
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "lane": "assistant_learning", "outcome_type": "observer_degraded", "source_ref": f"observer:{reason}", "outcome_status": "degraded", "confidence": 0.5, "evidence": {"reason": reason, "error_hash": stable_id(error, prefix="error")}, "source_refs": ["assistant_outcome_observer"], "safe_summary": reason}


def _lane_from_title(title: str, key: Any) -> str:
    text = f"{title} {key or ''}".lower()
    if "email" in text:
        return "email_triage"
    if "coding" in text:
        return "coding_focus"
    if "task" in text:
        return "task_focus"
    if "training" in text:
        return "training"
    return "calendar"


def _lane_from_job(job: Any) -> str:
    mapping = {"email_triage_booking": "email_triage", "coding_work_briefing": "coding_focus", "training_calendar_booking": "training", "task_spine": "task_spine", "task_command_capture": "task_command", "daily_personal_briefing": "daily_briefing", "assistant_learning": "assistant_learning"}
    return mapping.get(str(job or ""), str(job or "unknown"))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect sanitized Rocky outcome observations.")
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--learning-db", default=str(DEFAULT_LEARNING_DB_PATH), dest="learning_db")
    parser.add_argument("--scheduler-db", dest="scheduler_db")
    parser.add_argument("--calendar-state-db", dest="calendar_state_db")
    parser.add_argument("--ledger-path", dest="ledger_path")
    parser.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect_outcomes(since_days=args.since_days, live=args.live, learning_db_path=args.learning_db, scheduler_db_path=args.scheduler_db, calendar_state_db_path=args.calendar_state_db, ledger_path=args.ledger_path, write_audit=args.write_audit)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else f"Assistant outcomes: {payload.get('status')} count={payload.get('outcome_count')}")
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
