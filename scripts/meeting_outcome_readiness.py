#!/usr/bin/env python3
"""Read-only readiness gate for Rocky meeting outcome capture."""
from __future__ import annotations

import argparse
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

from assistant_launchd import inspect_launchagent
from assistant_scheduler_health import MEETING_OUTCOME_CAPTURE_SPEC
from assistant_scheduler_state import DEFAULT_SCHEDULER_DB_PATH, AssistantSchedulerState


READY_VERIFIED = "ready_verified"
READY_PENDING = "ready_pending_natural_run"
NOT_READY = "not_ready"
MANUAL_REVIEW = "manual_review_required"
TIMEZONE = "Europe/Prague"
SENSITIVE_RE = re.compile(r"(token|secret|password|credential|cookie|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


def evaluate_meeting_outcome_readiness(
    *,
    expected_date: str | date | None = None,
    now_local: str | datetime | None = None,
    scheduler_db_path: str | Path | None = None,
    state_file: str | Path | None = None,
    stderr_path: str | Path | None = None,
    launchagent_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tz = ZoneInfo(TIMEZONE)
    now = _parse_datetime(now_local, tz=tz) if now_local else datetime.now(tz)
    target = _parse_date(expected_date) if expected_date else now.date()
    spec = MEETING_OUTCOME_CAPTURE_SPEC
    expected_run = _expected_run(target)
    grace_until = expected_run + timedelta(minutes=60)
    status = READY_PENDING if now < grace_until else READY_VERIFIED
    reason = "meeting_outcome_first_natural_run_pending" if status == READY_PENDING else "meeting_outcome_natural_run_verified"
    launch = launchagent_payload or inspect_launchagent(spec.launchagent, now=now).to_dict()
    state_payload = _read_json(Path(state_file or spec.state_path or ""))
    runs = AssistantSchedulerState(scheduler_db_path or DEFAULT_SCHEDULER_DB_PATH).list_job_runs(job_name=spec.job_name, limit=10)
    recent_for_date = _recent_for_date(runs, target, expected_run=expected_run)
    stderr_status = _stderr_status(Path(stderr_path or spec.launchagent.stderr_path))
    issues: list[dict[str, Any]] = []
    if launch.get("status") == "blocked":
        issues.append({"source": "launchagent", "reason": launch.get("failure_class") or "launchagent_blocked"})
    if stderr_status.get("status") != "ok":
        issues.append({"source": "stderr", "reason": stderr_status.get("reason")})
    if now >= grace_until and not recent_for_date:
        issues.append({"source": "run_history", "reason": "meeting_outcome_run_missing_after_grace"})
    state_run_at = _parse_datetime(str(state_payload.get("last_run_at") or ""), tz=tz) if state_payload.get("last_run_at") else None
    if now >= grace_until and state_payload and str(state_payload.get("target_date") or "") != target.isoformat():
        issues.append({"source": "state", "reason": "meeting_outcome_state_stale", "state_target_date": state_payload.get("target_date")})
    if now >= grace_until and state_payload and state_run_at and state_run_at < expected_run:
        issues.append({"source": "state", "reason": "meeting_outcome_state_from_manual_pre_run", "state_last_run_at": state_payload.get("last_run_at")})
    if issues:
        status = NOT_READY if any(item["source"] in {"launchagent", "stderr", "run_history"} for item in issues) else MANUAL_REVIEW
        reason = "meeting_outcome_readiness_evidence_failed"
    return _redact({
        "status": status,
        "reason": reason,
        "production_ready": status == READY_VERIFIED,
        "summary": _summary(status, issues=issues, target=target),
        "expected_date": target.isoformat(),
        "expected_run": expected_run.isoformat(),
        "grace_until": grace_until.isoformat(),
        "evidence": {"launchagent": _safe_launch(launch), "stderr": stderr_status, "state": _safe_state(state_payload), "recent_run": _safe_run(recent_for_date), "recent_run_count": len(runs)},
        "issues": issues,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def _expected_run(day: date) -> datetime:
    candidate_day = day
    if day.weekday() >= 5:
        candidate_day = day + timedelta(days=7 - day.weekday())
    return datetime.combine(candidate_day, datetime.min.time().replace(hour=8, minute=0), tzinfo=ZoneInfo(TIMEZONE))


def _recent_for_date(runs: list[dict[str, Any]], target: date, *, expected_run: datetime) -> dict[str, Any] | None:
    for run in runs:
        summary = _parse_json(run.get("summary"))
        run_time = _parse_datetime(str(run.get("finished_at") or run.get("created_at") or ""), tz=ZoneInfo(TIMEZONE)) if (run.get("finished_at") or run.get("created_at")) else None
        if run_time and run_time < expected_run:
            continue
        if summary.get("target_date") == target.isoformat() or str(run.get("scheduled_for") or "")[:10] == target.isoformat():
            return run
    return None


def _stderr_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "ok", "reason": "stderr_log_missing_before_first_run", "chars": 0}
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return {"status": "blocked", "reason": "stderr_not_empty", "chars": len(text)} if text else {"status": "ok", "chars": 0}


def _safe_launch(payload: dict[str, Any]) -> dict[str, Any]:
    launchctl = payload.get("launchctl") or {}
    return {"status": payload.get("status"), "failure_class": payload.get("failure_class"), "issues": payload.get("issues") or [], "runs": launchctl.get("runs"), "last_exit_code": launchctl.get("last_exit_code")}


def _safe_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ("last_run_at", "last_status", "target_date", "reason", "processed_count", "tasks_created", "memory_promoted_count", "calendar_write_attempted", "notion_write_attempted") if key in payload}


def _safe_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {"run_id": run.get("run_id"), "status": run.get("status"), "idempotency_key": run.get("idempotency_key"), "created_at": run.get("created_at"), "summary": _parse_json(run.get("summary"))}


def _summary(status: str, *, issues: list[dict[str, Any]], target: date) -> str:
    if status == READY_VERIFIED:
        return f"Meeting outcome capture is production-proven for {target.isoformat()}."
    if status == READY_PENDING:
        return f"Meeting outcome capture is healthy but waiting for the first natural weekday run for {target.isoformat()}."
    return "Meeting outcome capture is not production-ready: " + ", ".join(str(item.get("reason")) for item in issues)


def _parse_datetime(value: str | datetime, *, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(tz)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_RE.sub("[redacted]", value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate meeting outcome readiness.")
    parser.add_argument("--expected-date")
    parser.add_argument("--now-local")
    parser.add_argument("--scheduler-db")
    parser.add_argument("--state-file")
    parser.add_argument("--stderr-path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = evaluate_meeting_outcome_readiness(expected_date=args.expected_date, now_local=args.now_local, scheduler_db_path=args.scheduler_db, state_file=args.state_file, stderr_path=args.stderr_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload.get("summary"))
    return 0 if payload.get("status") in {READY_VERIFIED, READY_PENDING} else 1


if __name__ == "__main__":
    raise SystemExit(main())
