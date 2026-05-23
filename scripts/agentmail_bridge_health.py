#!/usr/bin/env python3
"""Source/deploy health checks for the AgentMail bridge runtime."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_ROOT = Path("/Users/clawdbot/.openclaw")
SOURCE_ROOT = ROOT / "services"
SOURCE_BRIDGE_ROOT = SOURCE_ROOT / "agentmail-bridge"
SOURCE_SECURITY_ROOT = SOURCE_ROOT / "email-security"
RUNTIME_BRIDGE_ROOT = OPENCLAW_ROOT / "agentmail-bridge"
RUNTIME_SECURITY_ROOT = OPENCLAW_ROOT / "email-security"
LAUNCHAGENT_PATH = Path("/Users/clawdbot/Library/LaunchAgents/ai.openclaw.agentmail-bridge.plist")
LAUNCHAGENT_LABEL = "ai.openclaw.agentmail-bridge"
EXPECTED_PROGRAM_ARGUMENTS = [
    "/Users/clawdbot/.nvm/versions/node/v22.22.1/bin/node",
    "/Users/clawdbot/.openclaw/agentmail-bridge/bridge.mjs",
]
EXPECTED_WORKING_DIRECTORY = "/Users/clawdbot/.openclaw/agentmail-bridge"
DEPLOYABLE_FILES = (
    ("agentmail-bridge/bridge.mjs", RUNTIME_BRIDGE_ROOT / "bridge.mjs"),
    ("agentmail-bridge/test_email_security_bridge.mjs", RUNTIME_BRIDGE_ROOT / "test_email_security_bridge.mjs"),
    ("agentmail-bridge/package.json", RUNTIME_BRIDGE_ROOT / "package.json"),
    ("agentmail-bridge/package-lock.json", RUNTIME_BRIDGE_ROOT / "package-lock.json"),
    ("email-security/email_security.mjs", RUNTIME_SECURITY_ROOT / "email_security.mjs"),
    ("email-security/rules.json", RUNTIME_SECURITY_ROOT / "rules.json"),
)
SECRET_PATTERNS = re.compile(
    r"(token|secret|password|credential|cookie|api[_-]?key|bearer)\s*[:=]\s*[^\s,}]+",
    flags=re.IGNORECASE,
)


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: BaseException) -> str:
    text = SECRET_PATTERNS.sub(r"\1=[redacted]", str(exc))
    return text[:300]


def _load_plist(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:
        return {"status": "blocked", "failure_class": "launchagent_plist_unreadable", "error": _safe_error(exc)}
    return data if isinstance(data, dict) else {}


def _launchctl_status(label: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return {"loaded": False, "failure_class": "launchctl_failed", "error": _safe_error(exc)}
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return {
            "loaded": False,
            "failure_class": "launchagent_not_loaded",
            "output_hash": hashlib.sha256(text.encode()).hexdigest()[:16] if text else None,
        }
    state_match = re.search(r"^\s*state = ([^\n]+)$", text, flags=re.MULTILINE)
    runs_match = re.search(r"^\s*runs = ([0-9]+)$", text, flags=re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = ([^\n]+)$", text, flags=re.MULTILINE)
    return {
        "loaded": True,
        "state": state_match.group(1).strip() if state_match else "unknown",
        "runs": int(runs_match.group(1)) if runs_match else None,
        "last_exit_raw": exit_match.group(1).strip() if exit_match else None,
        "output_hash": hashlib.sha256(text.encode()).hexdigest()[:16] if text else None,
    }


def _node_test_status(*, run_tests: bool) -> dict[str, Any]:
    if not run_tests:
        return {"status": "skipped", "reason": "run_tests_false"}
    test_path = SOURCE_BRIDGE_ROOT / "test_email_security_bridge.mjs"
    if not test_path.is_file():
        return {"status": "blocked", "failure_class": "test_file_missing"}
    try:
        proc = subprocess.run(
            ["node", "--test", str(test_path)],
            cwd=str(SOURCE_BRIDGE_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return {"status": "blocked", "failure_class": "node_test_failed", "error": _safe_error(exc)}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "status": "ok" if proc.returncode == 0 else "blocked",
        "failure_class": None if proc.returncode == 0 else "node_test_failed",
        "returncode": proc.returncode,
        "output_hash": hashlib.sha256(output.encode()).hexdigest()[:16] if output else None,
        "output_chars": len(output),
    }


def build_agentmail_bridge_health(*, run_tests: bool = False, read_launchctl: bool = True) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    drifted: list[str] = []
    missing: list[str] = []
    for rel, deployed_path in DEPLOYABLE_FILES:
        source_path = SOURCE_ROOT / rel
        source_hash = _sha256_file(source_path)
        deployed_hash = _sha256_file(deployed_path)
        status = "ok" if source_hash and source_hash == deployed_hash else "drift"
        if not source_hash or not deployed_hash:
            status = "missing"
            missing.append(rel)
        elif source_hash != deployed_hash:
            drifted.append(rel)
        files.append(
            {
                "path": rel,
                "source_exists": bool(source_hash),
                "deployed_exists": bool(deployed_hash),
                "source_hash": source_hash,
                "deployed_hash": deployed_hash,
                "status": status,
            }
        )

    plist = _load_plist(LAUNCHAGENT_PATH) if LAUNCHAGENT_PATH.is_file() else {}
    launchagent_issues: list[str] = []
    if not LAUNCHAGENT_PATH.is_file():
        launchagent_issues.append("launchagent_plist_missing")
    elif plist.get("Label") != LAUNCHAGENT_LABEL:
        launchagent_issues.append("launchagent_label_mismatch")
    if plist and list(plist.get("ProgramArguments") or []) != EXPECTED_PROGRAM_ARGUMENTS:
        launchagent_issues.append("launchagent_program_mismatch")
    if plist and str(plist.get("WorkingDirectory") or "") != EXPECTED_WORKING_DIRECTORY:
        launchagent_issues.append("launchagent_working_directory_mismatch")

    launchctl = _launchctl_status(LAUNCHAGENT_LABEL) if read_launchctl else {"loaded": None, "status": "skipped"}
    node_tests = _node_test_status(run_tests=run_tests)

    if missing or launchagent_issues or node_tests.get("status") == "blocked" or launchctl.get("failure_class"):
        status = "blocked"
    elif drifted:
        status = "degraded"
    else:
        status = "ok"
    if missing:
        recommendation = "restore_tracked_source_or_deploy_runtime_files"
    elif drifted:
        recommendation = "run_agentmail_bridge_deploy_after_review"
    elif launchagent_issues:
        recommendation = "repair_agentmail_bridge_launchagent"
    elif node_tests.get("status") == "blocked":
        recommendation = "fix_agentmail_bridge_tests_before_deploy"
    else:
        recommendation = "none"
    return {
        "status": status,
        "service": "agentmail_bridge",
        "source_root": str(SOURCE_ROOT),
        "source_bridge_root": str(SOURCE_BRIDGE_ROOT),
        "source_security_root": str(SOURCE_SECURITY_ROOT),
        "runtime_bridge_root": str(RUNTIME_BRIDGE_ROOT),
        "runtime_security_root": str(RUNTIME_SECURITY_ROOT),
        "launchagent": {
            "label": LAUNCHAGENT_LABEL,
            "plist_path": str(LAUNCHAGENT_PATH),
            "program_arguments_match": "launchagent_program_mismatch" not in launchagent_issues,
            "working_directory_match": "launchagent_working_directory_mismatch" not in launchagent_issues,
            "issues": launchagent_issues,
        },
        "launchctl": launchctl,
        "files": files,
        "drifted_files": drifted,
        "missing_files": missing,
        "node_tests": node_tests,
        "recommendation": recommendation,
        "writes_attempted": [],
        "secrets_included": False,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check AgentMail bridge source/deploy health.")
    parser.add_argument("--run-tests", action="store_true", help="Run tracked Node tests as part of health.")
    parser.add_argument("--no-launchctl", action="store_false", dest="read_launchctl", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = build_agentmail_bridge_health(run_tests=args.run_tests, read_launchctl=args.read_launchctl)
    if args.json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"AgentMail bridge: {payload[status]} ({payload[recommendation]})")
        for item in payload["files"]:
            print(f"- {item[path]}: {item[status]}")
    return 0 if payload["status"] in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
