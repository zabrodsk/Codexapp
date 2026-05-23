import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_run_lock import RunLockResult
from assistant_scheduler_health_launcher import build_parser, main, run


def _args(**kwargs):
    defaults = {
        "job": "betty_mail_triage",
        "state_db": None,
        "audit_ledger": None,
        "lock_ttl_seconds": 600,
        "alert_mode": "none",
        "channel_id": None,
        "json_output": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _lock(*, acquired=True):
    return RunLockResult(
        status="acquired" if acquired else "duplicate_blocked",
        acquired=acquired,
        lock_key="lock:test",
        workflow="assistant_scheduler_health",
        idempotency_key="idem",
        reason="acquired" if acquired else "active_lock_exists",
    )


def test_parser_defaults_to_no_alerts():
    args = build_parser().parse_args(["--job", "betty_mail_triage", "--json"])

    assert args.job == "betty_mail_triage"
    assert args.alert_mode == "none"
    assert args.json_output is True


def test_run_health_check_success_releases_lock():
    with patch("assistant_scheduler_health_launcher.acquire_run_lock", return_value=_lock(acquired=True)), patch(
        "assistant_scheduler_health_launcher.release_run_lock", return_value=_lock(acquired=False)
    ) as release, patch(
        "assistant_scheduler_health_launcher.evaluate_all_scheduler_jobs",
        return_value={
            "status": "healthy",
            "jobs": [],
            "helpers_run": False,
            "notifications_sent": False,
            "calendar_write_attempted": False,
        },
    ):
        exit_code, payload = run(_args())

    assert exit_code == 0
    assert payload["status"] == "healthy"
    assert payload["helpers_run"] is False
    assert payload["notifications_sent"] is False
    assert payload["calendar_write_attempted"] is False
    release.assert_called_once()


def test_run_blocked_health_returns_nonzero():
    with patch("assistant_scheduler_health_launcher.acquire_run_lock", return_value=_lock(acquired=True)), patch(
        "assistant_scheduler_health_launcher.release_run_lock", return_value=_lock(acquired=False)
    ), patch(
        "assistant_scheduler_health_launcher.evaluate_all_scheduler_jobs",
        return_value={
            "status": "blocked",
            "jobs": [],
            "helpers_run": False,
            "notifications_sent": False,
            "calendar_write_attempted": False,
        },
    ):
        exit_code, payload = run(_args())

    assert exit_code == 1
    assert payload["status"] == "blocked"


def test_duplicate_lock_skips_without_running_health():
    with patch("assistant_scheduler_health_launcher.acquire_run_lock", return_value=_lock(acquired=False)), patch(
        "assistant_scheduler_health_launcher.evaluate_all_scheduler_jobs"
    ) as health:
        exit_code, payload = run(_args())

    assert exit_code == 0
    assert payload["status"] == "skipped_duplicate"
    assert payload["helpers_run"] is False
    health.assert_not_called()


def test_main_outputs_json(capsys):
    with patch(
        "assistant_scheduler_health_launcher.run",
        return_value=(
            0,
            {
                "status": "healthy",
                "jobs": [],
                "helpers_run": False,
                "notifications_sent": False,
                "calendar_write_attempted": False,
            },
        ),
    ):
        result = main(["--job", "betty_mail_triage", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "healthy"
