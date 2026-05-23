import json
import plistlib
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_launchd import LaunchAgentSpec
from assistant_scheduler_health import (
    EMAIL_TRIAGE_BOOKING_SPEC,
    EMAIL_TRIAGE_DIRECT_PROGRAM_ARGUMENTS,
    EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
    JOB_REGISTRY,
    CODING_WORK_BRIEFING_SPEC,
    CODING_WORK_DIRECT_PROGRAM_ARGUMENTS,
    CODING_WORK_SSH_BRIDGE_PROGRAM_ARGUMENTS,
    TASK_SPINE_DIRECT_PROGRAM_ARGUMENTS,
    TASK_SPINE_SPEC,
    TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
    TASK_COMMAND_CAPTURE_DIRECT_PROGRAM_ARGUMENTS,
    TASK_COMMAND_CAPTURE_SPEC,
    TRAINING_CALENDAR_DIRECT_PROGRAM_ARGUMENTS,
    TRAINING_CALENDAR_BOOKING_SPEC,
    TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS,
    SchedulerJobSpec,
    evaluate_scheduler_job,
    launchagent_execution_mode,
)


def _write_jobs(path: Path, *, enabled: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "old-cron",
                        "name": "old-cron-job",
                        "enabled": enabled,
                    }
                ]
            }
        )
    )


def _spec(tmp_path, *, first_expected="2026-05-25T09:00:00+02:00"):
    helper = tmp_path / "helper.py"
    helper.write_text("print('ok')\n")
    plist_path = tmp_path / "com.openclaw.test.plist"
    stdout = tmp_path / "out.log"
    stderr = tmp_path / "err.log"
    launchagent = LaunchAgentSpec(
        label="com.openclaw.test",
        plist_path=str(plist_path),
        program_arguments=["/usr/bin/python3", str(helper)],
        working_directory=str(tmp_path),
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        weekdays=[1, 2, 3, 4, 5],
        hour=9,
        minute=0,
        timezone="Europe/Prague",
        first_expected_run_after=first_expected,
    )
    plist_payload = {
        "Label": launchagent.label,
        "ProgramArguments": launchagent.program_arguments,
        "WorkingDirectory": launchagent.working_directory,
        "StandardOutPath": launchagent.stdout_path,
        "StandardErrorPath": launchagent.stderr_path,
        "StartCalendarInterval": [
            {"Weekday": day, "Hour": 9, "Minute": 0}
            for day in launchagent.weekdays
        ],
    }
    plist_path.write_bytes(plistlib.dumps(plist_payload))
    jobs_path = tmp_path / "jobs.json"
    _write_jobs(jobs_path, enabled=False)
    return SchedulerJobSpec(
        job_name="test_job",
        job_label="Test job",
        workflow="test_job",
        launchagent=launchagent,
        old_openclaw_cron_job_id="old-cron",
        old_openclaw_cron_job_name="old-cron-job",
        old_openclaw_cron_jobs_path=str(jobs_path),
        state_path=str(tmp_path / "state.json"),
        first_expected_run_after=first_expected,
    )


def test_pending_first_run_is_healthy_and_writes_audit(tmp_path):
    spec = _spec(tmp_path)
    payload = evaluate_scheduler_job(
        spec,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
        state_db_path=tmp_path / "scheduler.sqlite3",
        audit_log_path=tmp_path / "assistant_audit.jsonl",
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert payload["status"] == "healthy"
    assert payload["helpers_run"] is False
    assert payload["calendar_write_attempted"] is False
    rows = [json.loads(line) for line in (tmp_path / "assistant_audit.jsonl").read_text().splitlines()]
    assert rows[-1]["event_type"] == "scheduler.health_ok"


def test_enabled_old_cron_blocks_and_creates_dead_letter(tmp_path):
    spec = _spec(tmp_path)
    _write_jobs(Path(spec.old_openclaw_cron_jobs_path), enabled=True)

    payload = evaluate_scheduler_job(
        spec,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
        state_db_path=tmp_path / "scheduler.sqlite3",
        audit_log_path=tmp_path / "assistant_audit.jsonl",
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "old_cron_unexpected_enabled"
    assert payload["dead_letter"]["failure_class"] == "old_cron_unexpected_enabled"


def test_missing_logs_after_grace_blocks(tmp_path):
    spec = _spec(tmp_path, first_expected="2026-05-20T09:00:00+02:00")

    payload = evaluate_scheduler_job(
        spec,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
        state_db_path=tmp_path / "scheduler.sqlite3",
        audit_log_path=tmp_path / "assistant_audit.jsonl",
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "launchagent_log_missing"


def test_program_mismatch_blocks(tmp_path):
    spec = _spec(tmp_path)
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = ["/usr/bin/python3", "/tmp/wrong.py"]
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    payload = evaluate_scheduler_job(
        spec,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
        state_db_path=tmp_path / "scheduler.sqlite3",
        audit_log_path=tmp_path / "assistant_audit.jsonl",
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "launchagent_program_mismatch"


def test_training_calendar_booking_launchagent_spec_matches_production_schedule():
    spec = TRAINING_CALENDAR_BOOKING_SPEC

    assert JOB_REGISTRY["training_calendar_booking"] is spec
    assert spec.workflow == "training_calendar_scheduler"
    assert spec.launchagent.label == "com.openclaw.rocky-training-calendar-booking"
    assert spec.launchagent.program_arguments == TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS
    assert "--reconcile" in " ".join(spec.launchagent.program_arguments)
    assert "--fix-safe" in " ".join(spec.launchagent.program_arguments)
    assert "--notify-failures" in " ".join(spec.launchagent.program_arguments)
    assert launchagent_execution_mode(spec.launchagent.program_arguments) == "localhost_ssh_bridge"
    assert launchagent_execution_mode(TRAINING_CALENDAR_DIRECT_PROGRAM_ARGUMENTS) == "direct_launchd_python"
    assert spec.launchagent.working_directory == "/Users/clawdbot/.openclaw/workspace"
    assert spec.launchagent.stdout_path == "/Users/clawdbot/.openclaw/logs/rocky-training-calendar-booking.log"
    assert spec.launchagent.stderr_path == "/Users/clawdbot/.openclaw/logs/rocky-training-calendar-booking.err.log"
    assert spec.launchagent.weekdays == [1, 2, 3, 4, 5]
    assert spec.launchagent.hour == 6
    assert spec.launchagent.minute == 30
    assert spec.state_path == "/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json"


def test_coding_work_briefing_launchagent_spec_matches_production_schedule():
    spec = CODING_WORK_BRIEFING_SPEC

    assert JOB_REGISTRY["coding_work_briefing"] is spec
    assert spec.workflow == "coding_work_scheduler"
    assert spec.launchagent.label == "com.openclaw.rocky-coding-work-briefing"
    assert spec.launchagent.program_arguments == CODING_WORK_SSH_BRIDGE_PROGRAM_ARGUMENTS
    assert "coding_work_scheduler.py --live --notify --json" in " ".join(spec.launchagent.program_arguments)
    assert launchagent_execution_mode(spec.launchagent.program_arguments) == "localhost_ssh_bridge"
    assert launchagent_execution_mode(CODING_WORK_DIRECT_PROGRAM_ARGUMENTS) == "direct_launchd_python"
    assert spec.launchagent.weekdays == [1, 2, 3, 4]
    assert spec.launchagent.hour == 12
    assert spec.launchagent.minute == 5
    assert spec.state_path == "/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json"


def test_task_command_capture_launchagent_spec_matches_production_interval():
    spec = TASK_COMMAND_CAPTURE_SPEC

    assert JOB_REGISTRY["task_command_capture"] is spec
    assert spec.workflow == "task_command_capture_scheduler"
    assert spec.launchagent.label == "com.openclaw.rocky-task-command-capture"
    assert spec.launchagent.program_arguments == TASK_COMMAND_CAPTURE_DIRECT_PROGRAM_ARGUMENTS
    assert launchagent_execution_mode(spec.launchagent.program_arguments) == "direct_launchd_python"
    assert spec.launchagent.start_interval_seconds == 300
    assert spec.launchagent.working_directory == "/Users/clawdbot/.openclaw/workspace"
    assert spec.launchagent.stdout_path == "/Users/clawdbot/.openclaw/logs/rocky-task-command-capture.log"
    assert spec.launchagent.stderr_path == "/Users/clawdbot/.openclaw/logs/rocky-task-command-capture.err.log"
    assert spec.state_path == "/Users/clawdbot/.openclaw/state/task_command_capture_scheduler.json"


def test_training_calendar_health_reports_ssh_bridge_mode(tmp_path):
    spec = _spec(tmp_path)
    spec = SchedulerJobSpec(
        job_name="training_calendar_booking",
        job_label="Rocky training calendar booking",
        workflow="training_calendar_scheduler",
        launchagent=type(spec.launchagent)(
            label=spec.launchagent.label,
            plist_path=spec.launchagent.plist_path,
            program_arguments=TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS,
            working_directory=spec.launchagent.working_directory,
            stdout_path=spec.launchagent.stdout_path,
            stderr_path=spec.launchagent.stderr_path,
            weekdays=spec.launchagent.weekdays,
            hour=spec.launchagent.hour,
            minute=spec.launchagent.minute,
            timezone=spec.launchagent.timezone,
            first_expected_run_after=spec.launchagent.first_expected_run_after,
        ),
        state_path=str(tmp_path / "state.json"),
    )
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    with patch("assistant_scheduler_health._localhost_ssh_status", return_value={"status": "ok"}):
        payload = evaluate_scheduler_job(
            spec,
            now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
            write_state=False,
            write_audit=False,
            launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
        )

    assert payload["status"] == "healthy"
    assert payload["execution_mode"] == "localhost_ssh_bridge"
    assert payload["signals"]["localhost_ssh_bridge"]["status"] == "ok"


def test_training_calendar_health_blocks_when_ssh_bridge_unavailable(tmp_path):
    spec = _spec(tmp_path)
    spec = SchedulerJobSpec(
        job_name="training_calendar_booking",
        job_label="Rocky training calendar booking",
        workflow="training_calendar_scheduler",
        launchagent=type(spec.launchagent)(
            label=spec.launchagent.label,
            plist_path=spec.launchagent.plist_path,
            program_arguments=TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS,
            working_directory=spec.launchagent.working_directory,
            stdout_path=spec.launchagent.stdout_path,
            stderr_path=spec.launchagent.stderr_path,
            weekdays=spec.launchagent.weekdays,
            hour=spec.launchagent.hour,
            minute=spec.launchagent.minute,
            timezone=spec.launchagent.timezone,
            first_expected_run_after=spec.launchagent.first_expected_run_after,
        ),
        state_path=str(tmp_path / "state.json"),
    )
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = TRAINING_CALENDAR_SSH_BRIDGE_PROGRAM_ARGUMENTS
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    with patch(
        "assistant_scheduler_health._localhost_ssh_status",
        return_value={"status": "blocked", "failure_class": "localhost_ssh_unavailable"},
    ):
        payload = evaluate_scheduler_job(
            spec,
            now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
            write_state=False,
            write_audit=False,
            launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
        )

    assert payload["status"] == "blocked"
    assert payload["failure_class"] == "localhost_ssh_unavailable"


def test_email_triage_booking_launchagent_spec_matches_production_schedule():
    spec = EMAIL_TRIAGE_BOOKING_SPEC

    assert JOB_REGISTRY["email_triage_booking"] is spec
    assert spec.workflow == "email_triage_scheduler"
    assert spec.launchagent.label == "com.openclaw.rocky-email-triage-booking"
    assert spec.launchagent.program_arguments == EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS
    assert "--notify-failures" in " ".join(spec.launchagent.program_arguments)
    assert launchagent_execution_mode(spec.launchagent.program_arguments) == "localhost_ssh_bridge"
    assert launchagent_execution_mode(EMAIL_TRIAGE_DIRECT_PROGRAM_ARGUMENTS) == "direct_launchd_python"
    assert spec.launchagent.working_directory == "/Users/clawdbot/.openclaw/workspace"
    assert spec.launchagent.stdout_path == "/Users/clawdbot/.openclaw/logs/rocky-email-triage-booking.log"
    assert spec.launchagent.stderr_path == "/Users/clawdbot/.openclaw/logs/rocky-email-triage-booking.err.log"
    assert spec.launchagent.weekdays == [1, 2, 3, 4]
    assert spec.launchagent.hour == 9
    assert spec.launchagent.minute == 30
    assert spec.state_path == "/Users/clawdbot/.openclaw/state/email_triage_scheduler.json"


def test_email_triage_health_reports_ssh_bridge_mode(tmp_path):
    base = _spec(tmp_path)
    spec = SchedulerJobSpec(
        job_name="email_triage_booking",
        job_label="Rocky email triage booking",
        workflow="email_triage_scheduler",
        launchagent=type(base.launchagent)(
            label=base.launchagent.label,
            plist_path=base.launchagent.plist_path,
            program_arguments=EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
            working_directory=base.launchagent.working_directory,
            stdout_path=base.launchagent.stdout_path,
            stderr_path=base.launchagent.stderr_path,
            weekdays=[1, 2, 3, 4],
            hour=9,
            minute=30,
            timezone=base.launchagent.timezone,
            first_expected_run_after=base.launchagent.first_expected_run_after,
        ),
        state_path=str(tmp_path / "state.json"),
    )
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = EMAIL_TRIAGE_SSH_BRIDGE_PROGRAM_ARGUMENTS
    plist["StartCalendarInterval"] = [
        {"Weekday": day, "Hour": 9, "Minute": 30}
        for day in [1, 2, 3, 4]
    ]
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    with patch("assistant_scheduler_health._localhost_ssh_status", return_value={"status": "ok"}):
        payload = evaluate_scheduler_job(
            spec,
            now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
            write_state=False,
            write_audit=False,
            launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
        )

    assert payload["status"] == "healthy"
    assert payload["execution_mode"] == "localhost_ssh_bridge"


def test_task_spine_launchagent_spec_matches_production_schedule():
    spec = TASK_SPINE_SPEC

    assert JOB_REGISTRY["task_spine"] is spec
    assert spec.workflow == "task_spine_scheduler"
    assert spec.launchagent.label == "com.openclaw.rocky-task-spine"
    assert spec.launchagent.program_arguments == TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS
    assert "--notify" in " ".join(spec.launchagent.program_arguments)
    assert launchagent_execution_mode(spec.launchagent.program_arguments) == "localhost_ssh_bridge"
    assert launchagent_execution_mode(TASK_SPINE_DIRECT_PROGRAM_ARGUMENTS) == "direct_launchd_python"
    assert spec.launchagent.working_directory == "/Users/clawdbot/.openclaw/workspace"
    assert spec.launchagent.stdout_path == "/Users/clawdbot/.openclaw/logs/rocky-task-spine.log"
    assert spec.launchagent.stderr_path == "/Users/clawdbot/.openclaw/logs/rocky-task-spine.err.log"
    assert spec.launchagent.weekdays == [1, 2, 3, 4]
    assert spec.launchagent.hour == 10
    assert spec.launchagent.minute == 15
    assert spec.state_path == "/Users/clawdbot/.openclaw/state/task_spine_scheduler.json"


def test_task_spine_health_reports_ssh_bridge_mode(tmp_path):
    base = _spec(tmp_path)
    spec = SchedulerJobSpec(
        job_name="task_spine",
        job_label="Rocky personal task spine",
        workflow="task_spine_scheduler",
        launchagent=type(base.launchagent)(
            label=base.launchagent.label,
            plist_path=base.launchagent.plist_path,
            program_arguments=TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
            working_directory=base.launchagent.working_directory,
            stdout_path=base.launchagent.stdout_path,
            stderr_path=base.launchagent.stderr_path,
            weekdays=[1, 2, 3, 4],
            hour=10,
            minute=15,
            timezone=base.launchagent.timezone,
            first_expected_run_after=base.launchagent.first_expected_run_after,
        ),
        state_path=str(tmp_path / "state.json"),
    )
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS
    plist["StartCalendarInterval"] = [
        {"Weekday": day, "Hour": 10, "Minute": 15}
        for day in [1, 2, 3, 4]
    ]
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    with patch("assistant_scheduler_health._localhost_ssh_status", return_value={"status": "ok"}):
        payload = evaluate_scheduler_job(
            spec,
            now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
            write_state=False,
            write_audit=False,
            launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
        )

    assert payload["status"] == "healthy"
    assert payload["execution_mode"] == "localhost_ssh_bridge"


def test_task_spine_health_reports_degraded_llm_from_state(tmp_path):
    base = _spec(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_status": "degraded",
                "reason": "task_spine_completed_with_degraded_signals",
                "llm": {
                    "status": "degraded",
                    "reason": "task_llm_model_failed",
                    "provider": "openai_codex_oauth",
                    "model": None,
                    "error_hash": "abc123",
                },
            }
        )
    )
    spec = SchedulerJobSpec(
        job_name="task_spine",
        job_label="Rocky personal task spine",
        workflow="task_spine_scheduler",
        launchagent=type(base.launchagent)(
            label=base.launchagent.label,
            plist_path=base.launchagent.plist_path,
            program_arguments=TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS,
            working_directory=base.launchagent.working_directory,
            stdout_path=base.launchagent.stdout_path,
            stderr_path=base.launchagent.stderr_path,
            weekdays=[1, 2, 3, 4],
            hour=10,
            minute=15,
            timezone=base.launchagent.timezone,
            first_expected_run_after=base.launchagent.first_expected_run_after,
        ),
        state_path=str(state_path),
    )
    plist = plistlib.loads(Path(spec.launchagent.plist_path).read_bytes())
    plist["ProgramArguments"] = TASK_SPINE_SSH_BRIDGE_PROGRAM_ARGUMENTS
    plist["StartCalendarInterval"] = [{"Weekday": day, "Hour": 10, "Minute": 15} for day in [1, 2, 3, 4]]
    Path(spec.launchagent.plist_path).write_bytes(plistlib.dumps(plist))

    with patch("assistant_scheduler_health._localhost_ssh_status", return_value={"status": "ok"}):
        payload = evaluate_scheduler_job(
            spec,
            now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
            write_state=False,
            write_audit=False,
            launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
        )

    assert payload["status"] == "degraded"
    assert payload["failure_class"] == "task_llm_degraded"
    assert payload["signals"]["task_llm"]["reason"] == "task_llm_model_failed"
