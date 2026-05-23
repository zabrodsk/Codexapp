#!/usr/bin/env python3
"""Read-only scheduler health monitor for Rocky assistant jobs."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from assistant_audit_log import AssistantAuditLog
from assistant_launchd import LaunchAgentSpec, inspect_launchagent
from assistant_scheduler_state import AssistantSchedulerState, utc_now_iso


SCHEDULER_POLICY_VERSION = "rocky-scheduler-policy-v1"
OPENCLAW_ROOT = Path("/Users/clawdbot/.openclaw")
WORKSPACE_ROOT = OPENCLAW_ROOT / "workspace"
TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS = [
    "/usr/bin/ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "localhost",
    "cd /Users/clawdbot/.openclaw/workspace && /Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/training_calendar_scheduler.py --live --reconcile --fix-safe --notify-failures --json",
]
TRAINING_CALENDAR_DIRECT_PROGRAM_ARGUMENTS = [
    "/Users/clawdbot/.openclaw/workspace/.venv/bin/python",
    "/Users/clawdbot/.openclaw/workspace/scripts/training_calendar_scheduler.py",
    "--live",
    "--reconcile",
    "--fix-safe",
    "--notify-failures",
    "--json",
]
EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS = [
    "/usr/bin/ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "localhost",
    "cd /Users/clawdbot/.openclaw/workspace && /Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/email_triage_scheduler.py --live --notify-failures --json",
]
EMAIL_TRIAGE_DIRECT_PROGRAM_ARGUMENTS = [
    "/Users/clawdbot/.openclaw/workspace/.venv/bin/python",
    "/Users/clawdbot/.openclaw/workspace/scripts/email_triage_scheduler.py",
    "--live",
    "--notify-failures",
    "--json",
]
TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS = [
    "/usr/bin/ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "localhost",
    "cd /Users/clawdbot/.openclaw/workspace && /Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/task_spine_scheduler.py --live --notify --json",
]
TASK_SPINE_DIRECT_PROGRAM_ARGUMENTS = [
    "/Users/clawdbot/.openclaw/workspace/.venv/bin/python",
    "/Users/clawdbot/.openclaw/workspace/scripts/task_spine_scheduler.py",
    "--live",
    "--notify",
    "--json",
]
CODING_WORK_SSH_BRIDGE_PROGRAM_ARGUMENTS = [
    "/usr/bin/ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "localhost",
    "cd /Users/clawdbot/.openclaw/workspace && /Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/coding_work_scheduler.py --live --notify --json",
]
CODING_WORK_DIRECT_PROGRAM_ARGUMENTS = [
    "/Users/clawdbot/.openclaw/workspace/.venv/bin/python",
    "/Users/clawdbot/.openclaw/workspace/scripts/coding_work_scheduler.py",
    "--live",
    "--notify",
    "--json",
]


@dataclass(frozen=True)
class SchedulerJobSpec:
    job_name: str
    job_label: str
    workflow: str
    launchagent: LaunchAgentSpec
    old_openclaw_cron_job_id: str | None = None
    old_openclaw_cron_job_name: str | None = None
    old_openclaw_cron_jobs_path: str = str(OPENCLAW_ROOT / "cron" / "jobs.json")
    state_path: str | None = None
    first_expected_run_after: str | None = None
    missing_log_grace_minutes: int = 120


def training_calendar_launchagent_spec(*, execution_mode: str = "localhost_ssh_bridge") -> LaunchAgentSpec:
    if execution_mode == "localhost_ssh_bridge":
        program_arguments = TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS
    elif execution_mode == "direct_launchd_python":
        program_arguments = TRAINING_CALENDAR_DIRECT_PROGRAM_ARGUMENTS
    else:
        raise ValueError(f"Unsupported training calendar execution mode: {execution_mode}")
    return LaunchAgentSpec(
        label="com.openclaw.rocky-training-calendar-booking",
        plist_path="/Users/clawdbot/Library/LaunchAgents/com.openclaw.rocky-training-calendar-booking.plist",
        program_arguments=program_arguments,
        working_directory="/Users/clawdbot/.openclaw/workspace",
        stdout_path="/Users/clawdbot/.openclaw/logs/rocky-training-calendar-booking.log",
        stderr_path="/Users/clawdbot/.openclaw/logs/rocky-training-calendar-booking.err.log",
        weekdays=[1, 2, 3, 4, 5],
        hour=6,
        minute=30,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T06:30:00+02:00",
    )


BETTY_MAIL_TRIAGE_SPEC = SchedulerJobSpec(
    job_name="betty_mail_triage",
    job_label="Betty weekday mail triage",
    workflow="betty_mail_triage",
    launchagent=LaunchAgentSpec(
        label="com.openclaw.betty-mail-triage",
        plist_path="/Users/clawdbot/Library/LaunchAgents/com.openclaw.betty-mail-triage.plist",
        program_arguments=[
            "/usr/bin/python3",
            "/Users/clawdbot/.openclaw/workspace/scripts/betty_mail_triage_proxy.py",
        ],
        working_directory="/Users/clawdbot/.openclaw/workspace",
        stdout_path="/Users/clawdbot/.openclaw/logs/betty-mail-triage.log",
        stderr_path="/Users/clawdbot/.openclaw/logs/betty-mail-triage.err.log",
        weekdays=[1, 2, 3, 4, 5],
        hour=9,
        minute=0,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T09:00:00+02:00",
    ),
    old_openclaw_cron_job_id="77a8b46c-49aa-4d6d-9789-d00b8b3ba3dd",
    old_openclaw_cron_job_name="betty-weekday-mail-triage",
    state_path="/Users/clawdbot/.openclaw/state/betty_mail_triage_proxy.json",
    first_expected_run_after="2026-05-25T09:00:00+02:00",
)

TRAINING_CALENDAR_BOOKING_SPEC = SchedulerJobSpec(
    job_name="training_calendar_booking",
    job_label="Rocky training calendar booking",
    workflow="training_calendar_scheduler",
    launchagent=training_calendar_launchagent_spec(execution_mode="localhost_ssh_bridge"),
    state_path="/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json",
    first_expected_run_after="2026-05-25T06:30:00+02:00",
)

EMAIL_TRIAGE_BOOKING_SPEC = SchedulerJobSpec(
    job_name="email_triage_booking",
    job_label="Rocky email triage booking",
    workflow="email_triage_scheduler",
    launchagent=LaunchAgentSpec(
        label="com.openclaw.rocky-email-triage-booking",
        plist_path="/Users/clawdbot/Library/LaunchAgents/com.openclaw.rocky-email-triage-booking.plist",
        program_arguments=EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
        working_directory="/Users/clawdbot/.openclaw/workspace",
        stdout_path="/Users/clawdbot/.openclaw/logs/rocky-email-triage-booking.log",
        stderr_path="/Users/clawdbot/.openclaw/logs/rocky-email-triage-booking.err.log",
        weekdays=[1, 2, 3, 4],
        hour=9,
        minute=30,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T09:30:00+02:00",
    ),
    state_path="/Users/clawdbot/.openclaw/state/email_triage_scheduler.json",
    first_expected_run_after="2026-05-25T09:30:00+02:00",
)

TASK_SPINE_SPEC = SchedulerJobSpec(
    job_name="task_spine",
    job_label="Rocky personal task spine",
    workflow="task_spine_scheduler",
    launchagent=LaunchAgentSpec(
        label="com.openclaw.rocky-task-spine",
        plist_path="/Users/clawdbot/Library/LaunchAgents/com.openclaw.rocky-task-spine.plist",
        program_arguments=TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
        working_directory="/Users/clawdbot/.openclaw/workspace",
        stdout_path="/Users/clawdbot/.openclaw/logs/rocky-task-spine.log",
        stderr_path="/Users/clawdbot/.openclaw/logs/rocky-task-spine.err.log",
        weekdays=[1, 2, 3, 4],
        hour=10,
        minute=15,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T10:15:00+02:00",
    ),
    state_path="/Users/clawdbot/.openclaw/state/task_spine_scheduler.json",
    first_expected_run_after="2026-05-25T10:15:00+02:00",
)

CODING_WORK_BRIEFING_SPEC = SchedulerJobSpec(
    job_name="coding_work_briefing",
    job_label="Rocky coding work briefing",
    workflow="coding_work_scheduler",
    launchagent=LaunchAgentSpec(
        label="com.openclaw.rocky-coding-work-briefing",
        plist_path="/Users/clawdbot/Library/LaunchAgents/com.openclaw.rocky-coding-work-briefing.plist",
        program_arguments=CODING_WORK_SSH_BRIDGE_PROGRAM_ARGUMENTS,
        working_directory="/Users/clawdbot/.openclaw/workspace",
        stdout_path="/Users/clawdbot/.openclaw/logs/rocky-coding-work-briefing.log",
        stderr_path="/Users/clawdbot/.openclaw/logs/rocky-coding-work-briefing.err.log",
        weekdays=[1, 2, 3, 4],
        hour=12,
        minute=5,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T12:05:00+02:00",
    ),
    state_path="/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json",
    first_expected_run_after="2026-05-25T12:05:00+02:00",
)

JOB_REGISTRY = {
    BETTY_MAIL_TRIAGE_SPEC.job_name: BETTY_MAIL_TRIAGE_SPEC,
    TRAINING_CALENDAR_BOOKING_SPEC.job_name: TRAINING_CALENDAR_BOOKING_SPEC,
    EMAIL_TRIAGE_BOOKING_SPEC.job_name: EMAIL_TRIAGE_BOOKING_SPEC,
    TASK_SPINE_SPEC.job_name: TASK_SPINE_SPEC,
    CODING_WORK_BRIEFING_SPEC.job_name: CODING_WORK_BRIEFING_SPEC,
}


def stable_scheduler_key(*, job_name: str, date_key: str, signal: str) -> str:
    digest = hashlib.sha256(f"{job_name}:{date_key}:{signal}".encode("utf-8")).hexdigest()[:16]
    return f"scheduler:{job_name}:{date_key}:{digest}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_file_tail(path: Path, *, max_chars: int = 2000) -> str | None:
    if not path.exists():
        return None
    data = path.read_text(encoding="utf-8", errors="replace")
    tail = data[-max_chars:]
    return hashlib.sha256(tail.encode("utf-8")).hexdigest()[:16]


def launchagent_execution_mode(program_arguments: list[str]) -> str:
    joined = " ".join(program_arguments)
    if (
        program_arguments[:1] == ["/usr/bin/ssh"]
        and "localhost" in program_arguments
        and "training_calendar_scheduler.py --live" in joined
        and "--reconcile" in joined
        and "--fix-safe" in joined
        and "--notify-failures" in joined
        and "--json" in joined
    ):
        return "localhost_ssh_bridge"
    if (
        program_arguments[:1] == ["/usr/bin/ssh"]
        and "localhost" in program_arguments
        and "email_triage_scheduler.py --live" in joined
        and "--notify-failures" in joined
        and "--json" in joined
    ):
        return "localhost_ssh_bridge"
    if (
        program_arguments
        and program_arguments[0].endswith("/python")
        and any(arg.endswith("training_calendar_scheduler.py") for arg in program_arguments)
        and "--live" in program_arguments
        and "--reconcile" in program_arguments
        and "--fix-safe" in program_arguments
        and "--notify-failures" in program_arguments
        and "--json" in program_arguments
    ):
        return "direct_launchd_python"
    if (
        program_arguments
        and program_arguments[0].endswith("/python")
        and any(arg.endswith("email_triage_scheduler.py") for arg in program_arguments)
        and "--live" in program_arguments
        and "--notify-failures" in program_arguments
        and "--json" in program_arguments
    ):
        return "direct_launchd_python"
    if (
        program_arguments[:1] == ["/usr/bin/ssh"]
        and "localhost" in program_arguments
        and "task_spine_scheduler.py --live" in joined
        and "--notify" in joined
        and "--json" in joined
    ):
        return "localhost_ssh_bridge"
    if (
        program_arguments[:1] == ["/usr/bin/ssh"]
        and "localhost" in program_arguments
        and "coding_work_scheduler.py --live" in joined
        and "--notify" in joined
        and "--json" in joined
    ):
        return "localhost_ssh_bridge"
    if (
        program_arguments
        and program_arguments[0].endswith("/python")
        and any(arg.endswith("task_spine_scheduler.py") for arg in program_arguments)
        and "--live" in program_arguments
        and "--notify" in program_arguments
        and "--json" in program_arguments
    ):
        return "direct_launchd_python"
    if (
        program_arguments
        and program_arguments[0].endswith("/python")
        and any(arg.endswith("coding_work_scheduler.py") for arg in program_arguments)
        and "--live" in program_arguments
        and "--notify" in program_arguments
        and "--json" in program_arguments
    ):
        return "direct_launchd_python"
    return "custom"


def _hash_text(value: str) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else None


def _localhost_ssh_status() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost", "true"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - exact host failures vary.
        return {
            "status": "blocked",
            "failure_class": "localhost_ssh_unavailable",
            "returncode": None,
            "stderr_hash": _hash_text(str(exc)),
        }
    return {
        "status": "ok" if proc.returncode == 0 else "blocked",
        "failure_class": None if proc.returncode == 0 else "localhost_ssh_unavailable",
        "returncode": proc.returncode,
        "stderr_hash": _hash_text(proc.stderr or ""),
    }


def _old_cron_status(spec: SchedulerJobSpec) -> dict[str, Any]:
    if not spec.old_openclaw_cron_job_id:
        return {"status": "not_configured"}
    path = Path(spec.old_openclaw_cron_jobs_path)
    if not path.exists():
        return {
            "status": "unknown",
            "failure_class": "target_not_found",
            "summary": f"Cron jobs file not found at {path}",
        }
    try:
        data = _load_json(path)
    except Exception as exc:
        return {
            "status": "unknown",
            "failure_class": "unknown_error",
            "summary": f"Cron jobs file unreadable: {exc}",
        }
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        if str(job.get("id")) == spec.old_openclaw_cron_job_id:
            enabled = bool(job.get("enabled", True))
            return {
                "status": "enabled" if enabled else "disabled",
                "enabled": enabled,
                "job_id": spec.old_openclaw_cron_job_id,
                "job_name": str(job.get("name") or ""),
            }
    return {
        "status": "unknown",
        "failure_class": "target_not_found",
        "summary": f"Old cron job {spec.old_openclaw_cron_job_id} not found",
    }


def _log_status(spec: SchedulerJobSpec, *, now: datetime) -> dict[str, Any]:
    stdout = Path(spec.launchagent.stdout_path)
    stderr = Path(spec.launchagent.stderr_path)
    first_expected = _parse_iso(spec.first_expected_run_after)
    before_first_expected = bool(first_expected and now < first_expected)
    after_grace = bool(
        first_expected
        and now > first_expected + timedelta(minutes=spec.missing_log_grace_minutes)
    )
    stdout_exists = stdout.exists()
    stderr_exists = stderr.exists()
    stderr_size = stderr.stat().st_size if stderr_exists else 0
    result = {
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "stdout_exists": stdout_exists,
        "stderr_exists": stderr_exists,
        "stderr_size": stderr_size,
        "before_first_expected": before_first_expected,
        "after_first_run_grace": after_grace,
        "stderr_hash": _hash_file_tail(stderr) if stderr_exists and stderr_size else None,
    }
    if before_first_expected and not stdout_exists and not stderr_exists:
        result.update({"status": "pending_first_run", "summary": "Logs are not expected before first natural run."})
    elif after_grace and not stdout_exists and not stderr_exists:
        result.update(
            {
                "status": "blocked",
                "failure_class": "launchagent_log_missing",
                "summary": "Logs are missing after the first expected run grace window.",
            }
        )
    elif stderr_exists and stderr_size > 0:
        result.update(
            {
                "status": "degraded",
                "failure_class": "launchagent_stderr_present",
                "summary": "Stderr log contains content; inspect safe hash/reference before rerun.",
            }
        )
    else:
        result.update({"status": "healthy", "summary": "Log state is acceptable."})
    return result


def _helper_state(spec: SchedulerJobSpec) -> dict[str, Any]:
    if not spec.state_path:
        return {"status": "not_configured"}
    path = Path(spec.state_path)
    if not path.exists():
        return {"status": "missing", "state_path": str(path)}
    try:
        state = _load_json(path)
    except Exception as exc:
        return {
            "status": "degraded",
            "failure_class": "state_unreadable",
            "state_path": str(path),
            "summary": str(exc),
        }
    safe_keys = [
        "last_success_date",
        "last_success_at",
        "memory_path",
        "last_run_at",
        "last_status",
        "target_date",
        "idempotency_key",
        "run_idempotency_key",
        "reason",
        "created_count",
        "skipped_count",
        "blocked_count",
        "work_item_count",
        "error_hash",
        "llm",
    ]
    safe_state = {key: state.get(key) for key in safe_keys if key in state}
    return {"status": "ok", "state_path": str(path), "state": safe_state}


def _max_status(statuses: list[str]) -> str:
    order = {"healthy": 0, "degraded": 1, "unknown": 1, "blocked": 2}
    if not statuses:
        return "healthy"
    return max(statuses, key=lambda item: order.get(item, 1))


def evaluate_scheduler_job(
    spec: SchedulerJobSpec,
    *,
    now: datetime | None = None,
    state_db_path: Path | str | None = None,
    audit_log_path: Path | str | None = None,
    write_state: bool = True,
    write_audit: bool = True,
    launchctl_text: str | None = None,
    launchctl_returncode: int = 0,
    read_launchctl: bool = True,
) -> dict[str, Any]:
    tz = ZoneInfo(spec.launchagent.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)
    date_key = now.date().isoformat()
    issues: list[dict[str, Any]] = []
    signals: dict[str, Any] = {}

    inspection = inspect_launchagent(
        spec.launchagent,
        now=now,
        launchctl_text=launchctl_text,
        launchctl_returncode=launchctl_returncode,
        read_launchctl=read_launchctl,
    )
    signals["launchagent"] = inspection.to_dict()
    actual_arguments = list(signals["launchagent"].get("plist", {}).get("ProgramArguments") or spec.launchagent.program_arguments)
    execution_mode = launchagent_execution_mode(actual_arguments)
    signals["execution_mode"] = {
        "mode": execution_mode,
        "status": "ok" if execution_mode != "custom" else "degraded",
    }
    if inspection.status == "blocked":
        issues.append(
            {
                "status": "blocked",
                "failure_class": inspection.failure_class or "launchagent_not_loaded",
                "summary": ", ".join(inspection.issues) or "LaunchAgent is blocked.",
            }
        )

    if execution_mode == "localhost_ssh_bridge":
        bridge = _localhost_ssh_status()
        signals["localhost_ssh_bridge"] = bridge
        if bridge.get("status") != "ok":
            issues.append(
                {
                    "status": "blocked",
                    "failure_class": bridge.get("failure_class") or "localhost_ssh_unavailable",
                    "summary": f"{spec.job_label} LaunchAgent uses the localhost SSH bridge, but localhost SSH is unavailable.",
                }
            )
    if spec.job_name in {"training_calendar_booking", "email_triage_booking", "task_spine", "coding_work_briefing"}:
        if execution_mode == "localhost_ssh_bridge":
            pass
        elif execution_mode == "custom":
            issues.append(
                {
                    "status": "degraded",
                    "failure_class": "launchagent_execution_mode_unknown",
                    "summary": f"{spec.job_label} LaunchAgent execution mode is not recognized as direct Python or localhost SSH bridge.",
                }
            )
    elif inspection.status == "degraded":
        issues.append(
            {
                "status": "degraded",
                "failure_class": inspection.failure_class or "launchagent_mismatch",
                "summary": ", ".join(inspection.issues) or "LaunchAgent is degraded.",
            }
        )

    old_cron = _old_cron_status(spec)
    signals["old_openclaw_cron"] = old_cron
    if old_cron.get("enabled"):
        issues.append(
            {
                "status": "blocked",
                "failure_class": "old_cron_unexpected_enabled",
                "summary": "Old OpenClaw cron job is unexpectedly enabled.",
            }
        )
    elif old_cron.get("status") == "unknown":
        issues.append(
            {
                "status": "degraded",
                "failure_class": old_cron.get("failure_class") or "unknown_error",
                "summary": old_cron.get("summary") or "Old cron status is unknown.",
            }
        )

    logs = _log_status(spec, now=now)
    signals["logs"] = logs
    if logs["status"] in {"blocked", "degraded"}:
        issues.append(
            {
                "status": logs["status"],
                "failure_class": logs.get("failure_class"),
                "summary": logs.get("summary"),
            }
        )

    proxy_state = _helper_state(spec)
    signals["helper_state"] = proxy_state
    if proxy_state.get("status") == "degraded":
        issues.append(
            {
                "status": "degraded",
                "failure_class": proxy_state.get("failure_class"),
                "summary": proxy_state.get("summary") or "Helper state is degraded.",
            }
        )
    if spec.job_name in {"training_calendar_booking", "email_triage_booking", "task_spine", "coding_work_briefing"}:
        helper_payload = proxy_state.get("state") or {}
        if helper_payload.get("error_hash"):
            issues.append(
                {
                    "status": "degraded",
                    "failure_class": f"{spec.job_name}_error_hash_present",
                    "summary": f"{spec.job_label} scheduler state has an error hash; inspect the safe state and recent dead letters.",
                }
            )
        if spec.job_name == "task_spine":
            llm_payload = helper_payload.get("llm") or {}
            signals["task_llm"] = {
                "status": llm_payload.get("status") or "unknown",
                "reason": llm_payload.get("reason"),
                "provider": llm_payload.get("provider"),
                "model": llm_payload.get("model"),
                "error_hash": llm_payload.get("error_hash"),
            }
            if llm_payload.get("status") == "degraded":
                issues.append(
                    {
                        "status": "degraded",
                        "failure_class": "task_llm_degraded",
                        "summary": f"{spec.job_label} task extraction LLM is degraded; Rocky is relying on deterministic fallback.",
                    }
                )

    overall_status = _max_status([issue["status"] for issue in issues])
    failure_class = next(
        (issue.get("failure_class") for issue in issues if issue["status"] == "blocked"),
        None,
    ) or next((issue.get("failure_class") for issue in issues), None)
    if not issues:
        summary = f"{spec.job_label} scheduler health is ok."
    else:
        summary = "; ".join(str(issue.get("summary") or issue.get("failure_class")) for issue in issues)

    idempotency_key = stable_scheduler_key(
        job_name=spec.job_name,
        date_key=date_key,
        signal=overall_status,
    )
    payload = {
        "job_name": spec.job_name,
        "job_label": spec.job_label,
        "workflow": spec.workflow,
        "execution_mode": execution_mode,
        "status": overall_status,
        "failure_class": failure_class,
        "summary": summary,
        "checked_at": now.isoformat(),
        "idempotency_key": idempotency_key,
        "issues": issues,
        "signals": signals,
        "side_effects": (["local_scheduler_db"] if write_state else [])
        + (["assistant_audit_log"] if write_audit else []),
        "helpers_run": False,
        "notifications_sent": False,
        "calendar_write_attempted": False,
    }

    if write_state:
        state = AssistantSchedulerState(state_db_path)
        run_status = {
            "healthy": "succeeded",
            "degraded": "stale",
            "blocked": "dead_lettered",
            "unknown": "unknown",
        }.get(overall_status, "unknown")
        state.record_job_run(
            job_name=spec.job_name,
            job_label=spec.job_label,
            scheduled_for=signals["launchagent"].get("previous_expected_run"),
            status=run_status,
            idempotency_key=idempotency_key,
            launchagent_label=spec.launchagent.label,
            program=spec.launchagent.program,
            exit_code=signals["launchagent"].get("launchctl", {}).get("last_exit_code"),
            failure_class=failure_class,
            summary=summary,
            error_hash=logs.get("stderr_hash"),
        )
        if overall_status == "blocked":
            dead_letter = state.upsert_dead_letter(
                job_name=spec.job_name,
                workflow=spec.workflow,
                idempotency_key=idempotency_key,
                failure_class=failure_class or "unknown_error",
                safe_summary=summary,
                source_refs=[spec.launchagent.plist_path, spec.old_openclaw_cron_jobs_path],
                recovery_hint="Inspect LaunchAgent, old cron state, and helper logs before rerunning.",
                error_hash=logs.get("stderr_hash"),
            )
            payload["dead_letter"] = dead_letter

    if write_audit:
        audit_log = AssistantAuditLog(audit_log_path)
        event_type = {
            "healthy": "scheduler.health_ok",
            "degraded": "scheduler.health_degraded",
            "blocked": "scheduler.health_blocked",
            "unknown": "scheduler.health_degraded",
        }.get(overall_status, "scheduler.health_degraded")
        decision = {
            "healthy": "allowed",
            "degraded": "degraded",
            "blocked": "blocked",
            "unknown": "degraded",
        }.get(overall_status, "degraded")
        event = audit_log.record_event(
            event_type=event_type,
            workflow=spec.workflow,
            idempotency_key=idempotency_key,
            policy_version=SCHEDULER_POLICY_VERSION,
            decision=decision,
            reason=summary,
            sources=[spec.launchagent.plist_path, spec.old_openclaw_cron_jobs_path],
            artifacts={
                "job_name": spec.job_name,
                "status": overall_status,
                "failure_class": failure_class,
                "signals": signals,
            },
        )
        payload["audit_id"] = event.audit_id
    return payload


def evaluate_all_scheduler_jobs(
    *,
    job_name: str | None = None,
    now: datetime | None = None,
    state_db_path: Path | str | None = None,
    audit_log_path: Path | str | None = None,
    write_state: bool = True,
    write_audit: bool = True,
) -> dict[str, Any]:
    specs = [JOB_REGISTRY[job_name]] if job_name else list(JOB_REGISTRY.values())
    jobs = [
        evaluate_scheduler_job(
            spec,
            now=now,
            state_db_path=state_db_path,
            audit_log_path=audit_log_path,
            write_state=write_state,
            write_audit=write_audit,
        )
        for spec in specs
    ]
    status = _max_status([job["status"] for job in jobs])
    return {
        "status": status,
        "checked_at": utc_now_iso(),
        "jobs": jobs,
        "helpers_run": False,
        "notifications_sent": False,
        "calendar_write_attempted": False,
    }


def format_scheduler_health_report(payload: dict[str, Any]) -> str:
    lines = ["Assistant Scheduler Health", "=" * 28]
    status_icons = {"healthy": "[OK]", "degraded": "[WARN]", "blocked": "[FAIL]", "unknown": "[????]"}
    for job in payload.get("jobs") or []:
        icon = status_icons.get(job.get("status"), "[????]")
        lines.append(f"{icon} {job.get('job_name')}: {job.get('summary')}")
        dead_letter = job.get("dead_letter")
        if dead_letter:
            lines.append(f"     -> dead_letter: {dead_letter.get('dead_letter_id')}")
    lines.append("")
    lines.append(f"Summary: {payload.get('status')}")
    return "\n".join(lines)
