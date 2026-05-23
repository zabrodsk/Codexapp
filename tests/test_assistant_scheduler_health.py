import json
import plistlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_launchd import LaunchAgentSpec
from assistant_scheduler_health import SchedulerJobSpec, evaluate_scheduler_job


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
