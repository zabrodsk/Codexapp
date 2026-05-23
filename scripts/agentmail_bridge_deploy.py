#!/usr/bin/env python3
"""Deploy tracked AgentMail bridge files to the OpenClaw runtime directory."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path

from agentmail_bridge_health import DEPLOYABLE_FILES, RUNTIME_BRIDGE_ROOT, RUNTIME_SECURITY_ROOT, SOURCE_ROOT, build_agentmail_bridge_health

BACKUP_ROOT = Path("/Users/clawdbot/.openclaw/backups/agentmail-bridge")


def _backup_file(path: Path, backup_dir: Path) -> str | None:
    if not path.exists():
        return None
    rel = path.relative_to(Path("/Users/clawdbot/.openclaw"))
    target = backup_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return str(target)


def deploy_agentmail_bridge(*, apply: bool = False) -> dict:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = BACKUP_ROOT / timestamp
    operations = []
    for rel, deployed_path in DEPLOYABLE_FILES:
        source_path = SOURCE_ROOT / rel
        if not source_path.is_file():
            return {
                "status": "blocked",
                "reason": "source_file_missing",
                "missing_source": rel,
                "applied": False,
                "operations": operations,
            }
        operation = {
            "source": str(source_path),
            "destination": str(deployed_path),
            "backup": None,
            "applied": False,
        }
        if apply:
            operation["backup"] = _backup_file(deployed_path, backup_dir)
            deployed_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, deployed_path)
            operation["applied"] = True
        operations.append(operation)
    health = build_agentmail_bridge_health(run_tests=False, read_launchctl=False)
    return {
        "status": "ok" if not apply or health["status"] in {"ok", "degraded"} else health["status"],
        "reason": "deploy_applied" if apply else "dry_run_only",
        "applied": apply,
        "source_root": str(SOURCE_ROOT),
        "runtime_bridge_root": str(RUNTIME_BRIDGE_ROOT),
        "runtime_security_root": str(RUNTIME_SECURITY_ROOT),
        "backup_dir": str(backup_dir) if apply else None,
        "operations": operations,
        "post_deploy_health": health if apply else None,
        "secrets_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy tracked AgentMail bridge files to runtime paths.")
    parser.add_argument("--apply", action="store_true", help="Actually copy files. Without this, dry-run only.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = deploy_agentmail_bridge(apply=args.apply)
    if args.json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"AgentMail bridge deploy: {payload[status]} ({payload[reason]})")
        for op in payload["operations"]:
            marker = "copied" if op["applied"] else "would copy"
            print(f"- {marker}: {op[source]} -> {op[destination]}")
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
