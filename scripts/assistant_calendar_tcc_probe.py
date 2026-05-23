#!/usr/bin/env python3
"""Read-only Calendar/TCC probe for Rocky launchd execution contexts."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from apple_calendar_cli import DEFAULT_DB_PATH
from assistant_calendar_status import calendar_write_health


WORKFLOW = "calendar_tcc_probe"
PROBE_LABEL = "com.openclaw.rocky-calendar-tcc-probe"
WORKSPACE_ROOT = Path("/Users/clawdbot/.openclaw/workspace")
VENV_PYTHON = WORKSPACE_ROOT / ".venv" / "bin" / "python"
PROBE_SCRIPT = WORKSPACE_ROOT / "scripts" / "assistant_calendar_tcc_probe.py"
PROBE_STDOUT_PATH = "/Users/clawdbot/.openclaw/logs/rocky-calendar-tcc-probe.log"
PROBE_STDERR_PATH = "/Users/clawdbot/.openclaw/logs/rocky-calendar-tcc-probe.err.log"

SENSITIVE_TEXT_RE = re.compile(
    r"("
    r"webcal://\S+|"
    r"https?://\S*(?:token|secret|password|credential|cookie)\S*|"
    r"(?:token|secret|password|credential|cookie)\s*[:=]\s*\S+|"
    r"authorization\s*:\s*\S+|"
    r"bearer\s+\S+"
    r")",
    re.IGNORECASE,
)
SENSITIVE_KEY_RE = re.compile(r"(cookie|credential|password|secret|token|webcal)", re.IGNORECASE)


def _redacted(value: Any) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return {
        "redacted": True,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "chars": len(text),
    }


def sanitize_probe_payload(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and SENSITIVE_KEY_RE.search(parent_key):
        return _redacted(value)
    if isinstance(value, dict):
        return {
            str(key): sanitize_probe_payload(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_probe_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [sanitize_probe_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        return _redacted(value)
    return value


def probe_launchagent_plist() -> dict[str, Any]:
    return {
        "Label": PROBE_LABEL,
        "ProgramArguments": [
            str(VENV_PYTHON),
            str(PROBE_SCRIPT),
            "--json",
        ],
        "WorkingDirectory": str(WORKSPACE_ROOT),
        "StandardOutPath": PROBE_STDOUT_PATH,
        "StandardErrorPath": PROBE_STDERR_PATH,
        "RunAtLoad": False,
    }


def _execution_context() -> str:
    xpc_service = os.environ.get("XPC_SERVICE_NAME")
    if xpc_service and xpc_service != "0":
        return "launchd"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return "ssh"
    return "interactive"


def _safe_context() -> dict[str, Any]:
    executable = Path(sys.executable)
    return {
        "execution_context": _execution_context(),
        "python_executable": str(executable),
        "python_realpath": str(executable.resolve()) if executable.exists() else str(executable),
        "uid": os.getuid(),
        "user": getpass.getuser(),
        "cwd": os.getcwd(),
        "xpc_service_name": os.environ.get("XPC_SERVICE_NAME"),
        "ssh_connection_present": bool(os.environ.get("SSH_CONNECTION")),
        "ssh_tty_present": bool(os.environ.get("SSH_TTY")),
    }


def _check_text(check: Any) -> str:
    return json.dumps(check, sort_keys=True, ensure_ascii=False, default=str).lower()


def classify_calendar_access_failure(health: dict[str, Any]) -> str | None:
    if health.get("status") == "ok":
        return None

    checks = health.get("checks") or {}
    blocked_checks = set(health.get("blocked_checks") or [])
    combined = _check_text(checks)
    tcc_markers = [
        "authorization denied",
        "not authorized",
        "not authorised",
        "operation not permitted",
        "not permitted",
        "privacy",
        "tcc",
        "-1743",
    ]
    if any(marker in combined for marker in tcc_markers):
        return "calendar_tcc_blocked"

    eventkit = checks.get("eventkit") or {}
    if str(eventkit.get("authorization") or "").lower() in {"denied", "restricted"}:
        return "calendar_tcc_blocked"
    if str(eventkit.get("authorization_raw") or "").strip() in {"1", "2"}:
        return "calendar_tcc_blocked"

    calendar_db = checks.get("calendar_db") or {}
    if "calendar_db" in blocked_checks and calendar_db.get("error") == "calendar_db_missing":
        return "calendar_db_missing"
    if "swift" in blocked_checks:
        return "swift_missing"
    if "eventkit" in blocked_checks:
        return "eventkit_unavailable"
    if "applescript_calendar" in blocked_checks:
        return "applescript_calendar_unavailable"
    return "calendar_access_blocked"


def _recommendation(failure_class: str | None) -> str:
    if failure_class is None:
        return "Calendar access is available in this execution context."
    if failure_class == "calendar_tcc_blocked":
        return (
            "Keep the verified localhost SSH bridge or grant the direct launchd "
            "Python executable the required macOS Calendar, Automation, or Full Disk Access permission; rerun the probe before switching production."
        )
    if failure_class == "calendar_db_missing":
        return "Confirm the Apple Calendar database path and rerun the probe from the target user account."
    return "Inspect the blocked probe checks before changing the production LaunchAgent."


def build_calendar_tcc_probe(*, db_path: Path | str | None = None) -> dict[str, Any]:
    health = calendar_write_health(
        db_path=Path(db_path).expanduser() if db_path else DEFAULT_DB_PATH,
        write_audit=False,
    )
    failure_class = classify_calendar_access_failure(health)
    status = "ok" if failure_class is None else "blocked"
    payload = {
        "status": status,
        "failure_class": failure_class,
        "reason": "calendar_access_ok" if status == "ok" else failure_class,
        "recommendation": _recommendation(failure_class),
        "workflow": WORKFLOW,
        "context": _safe_context(),
        "blocked_checks": health.get("blocked_checks") or [],
        "checks": health.get("checks") or {},
        "diagnostic_launchagent": probe_launchagent_plist(),
        "calendar_write_attempted": False,
        "calendar_event_created": False,
        "calendar_event_deleted": False,
        "side_effects": [],
    }
    return sanitize_probe_payload(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Rocky's read-only Calendar TCC probe.")
    parser.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output raw JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_calendar_tcc_probe(db_path=args.db_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar TCC probe: {payload.get('status')}")
        print(f"Failure class: {payload.get('failure_class')}")
        print(f"Context: {(payload.get('context') or {}).get('execution_context')}")
        print(f"Recommendation: {payload.get('recommendation')}")
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROBE_LABEL",
    "build_calendar_tcc_probe",
    "classify_calendar_access_failure",
    "probe_launchagent_plist",
    "sanitize_probe_payload",
]
