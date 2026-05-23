import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_tcc_probe import (
    build_calendar_tcc_probe,
    classify_calendar_access_failure,
    probe_launchagent_plist,
)


def test_tcc_probe_returns_sanitized_ok_without_calendar_writes():
    with patch(
        "assistant_calendar_tcc_probe.calendar_write_health",
        return_value={
            "status": "ok",
            "blocked_checks": [],
            "checks": {
                "calendar_db": {"status": "ok", "sample_event_count": 0},
                "eventkit": {"status": "ok", "authorization": "authorized"},
                "applescript_calendar": {"status": "ok", "calendar_count": 3},
            },
            "calendar_write_attempted": False,
        },
    ) as health:
        payload = build_calendar_tcc_probe()

    assert payload["status"] == "ok"
    assert payload["failure_class"] is None
    assert payload["calendar_write_attempted"] is False
    assert payload["calendar_event_created"] is False
    health.assert_called_once()
    assert health.call_args.kwargs["write_audit"] is False


def test_tcc_probe_classifies_authorization_denied_as_calendar_tcc_blocked():
    payload = {
        "status": "blocked",
        "blocked_checks": ["calendar_db"],
        "checks": {
            "calendar_db": {
                "status": "blocked",
                "error": "DatabaseError: authorization denied",
            }
        },
    }

    assert classify_calendar_access_failure(payload) == "calendar_tcc_blocked"


def test_tcc_probe_redacts_urls_tokens_and_cookies_from_output():
    with patch(
        "assistant_calendar_tcc_probe.calendar_write_health",
        return_value={
            "status": "blocked",
            "blocked_checks": ["calendar_db"],
            "checks": {
                "calendar_db": {
                    "status": "blocked",
                    "error": "webcal://example.invalid/private-token token=abc cookie=session",
                }
            },
            "calendar_write_attempted": False,
        },
    ):
        payload = build_calendar_tcc_probe()

    rendered = json.dumps(payload)
    assert "webcal://example.invalid" not in rendered
    assert "token=abc" not in rendered
    assert "cookie=session" not in rendered
    assert "redacted" in rendered


def test_probe_launchagent_plist_uses_direct_venv_python_not_ssh():
    plist = probe_launchagent_plist()

    assert plist["Label"] == "com.openclaw.rocky-calendar-tcc-probe"
    assert plist["ProgramArguments"][0].endswith("/.venv/bin/python")
    assert "ssh" not in plist["ProgramArguments"][0]
    assert plist["ProgramArguments"][1].endswith("assistant_calendar_tcc_probe.py")
