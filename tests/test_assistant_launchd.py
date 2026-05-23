import plistlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_launchd import (
    LaunchAgentSpec,
    expected_run_bounds,
    inspect_launchagent,
    normalize_start_calendar_interval,
    parse_launchctl_print,
)


def _write_plist(path: Path, spec: LaunchAgentSpec):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": spec.label,
        "ProgramArguments": spec.program_arguments,
        "WorkingDirectory": spec.working_directory,
        "StandardOutPath": spec.stdout_path,
        "StandardErrorPath": spec.stderr_path,
        "StartCalendarInterval": [
            {"Weekday": day, "Hour": spec.hour, "Minute": spec.minute}
            for day in spec.weekdays
        ],
    }
    path.write_bytes(plistlib.dumps(payload))


def _spec(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("print('ok')\n")
    return LaunchAgentSpec(
        label="com.openclaw.test",
        plist_path=str(tmp_path / "com.openclaw.test.plist"),
        program_arguments=["/usr/bin/python3", str(helper)],
        working_directory=str(tmp_path),
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
        weekdays=[1, 2, 3, 4, 5],
        hour=9,
        minute=0,
        timezone="Europe/Prague",
        first_expected_run_after="2026-05-25T09:00:00+02:00",
    )


def test_normalize_start_calendar_interval_sorts_weekdays():
    value = [
        {"Minute": 0, "Hour": 9, "Weekday": 5},
        {"Minute": 0, "Hour": 9, "Weekday": 1},
    ]

    assert normalize_start_calendar_interval(value)[0]["Weekday"] == 1


def test_expected_run_bounds_use_launchd_weekday_semantics():
    spec = LaunchAgentSpec(
        label="x",
        plist_path="/tmp/x",
        program_arguments=[],
        working_directory="/tmp",
        stdout_path="/tmp/out",
        stderr_path="/tmp/err",
        weekdays=[1, 2, 3, 4, 5],
        hour=9,
        minute=0,
    )
    now = datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague"))

    previous, next_run = expected_run_bounds(spec, now=now)

    assert previous.isoformat() == "2026-05-22T09:00:00+02:00"
    assert next_run.isoformat() == "2026-05-25T09:00:00+02:00"


def test_parse_launchctl_print_loaded_state():
    text = """
gui/501/com.openclaw.test = {
    state = not running
    runs = 0
    last exit code = (never exited)
}
"""

    status = parse_launchctl_print(text)

    assert status.loaded is True
    assert status.state == "not running"
    assert status.runs == 0
    assert status.last_exit_code is None


def test_inspect_launchagent_pending_first_run_is_healthy(tmp_path):
    spec = _spec(tmp_path)
    _write_plist(Path(spec.plist_path), spec)

    inspection = inspect_launchagent(
        spec,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=ZoneInfo("Europe/Prague")),
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert inspection.status == "healthy"
    assert inspection.failure_class is None


def test_inspect_launchagent_missing_plist_blocks(tmp_path):
    spec = _spec(tmp_path)

    inspection = inspect_launchagent(
        spec,
        launchctl_text="state = not running\nruns = 0\nlast exit code = (never exited)\n",
    )

    assert inspection.status == "blocked"
    assert inspection.failure_class == "launchagent_plist_missing"
