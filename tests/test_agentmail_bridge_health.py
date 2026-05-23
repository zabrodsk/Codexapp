import json
import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agentmail_bridge_health as health


def _write(path: Path, text: str = "same"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_plist(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": health.LAUNCHAGENT_LABEL,
                "ProgramArguments": health.EXPECTED_PROGRAM_ARGUMENTS,
                "WorkingDirectory": health.EXPECTED_WORKING_DIRECTORY,
            }
        )
    )


def test_agentmail_bridge_health_reports_matching_source_and_runtime(tmp_path):
    source_root = tmp_path / "source"
    runtime = tmp_path / "runtime"
    security = tmp_path / "security"
    plist = tmp_path / "bridge.plist"
    files = (
        ("agentmail-bridge/bridge.mjs", runtime / "bridge.mjs"),
        ("email-security/email_security.mjs", security / "email_security.mjs"),
    )
    for rel, deployed in files:
        _write(source_root / rel, "same")
        _write(deployed, "same")
    _write_plist(plist)

    with patch.object(health, "SOURCE_ROOT", source_root), patch.object(health, "SOURCE_BRIDGE_ROOT", source_root / "agentmail-bridge"), patch.object(health, "SOURCE_SECURITY_ROOT", source_root / "email-security"), patch.object(health, "RUNTIME_BRIDGE_ROOT", runtime), patch.object(health, "RUNTIME_SECURITY_ROOT", security), patch.object(health, "LAUNCHAGENT_PATH", plist), patch.object(health, "DEPLOYABLE_FILES", files):
        payload = health.build_agentmail_bridge_health(read_launchctl=False)

    assert payload["status"] == "ok"
    assert payload["drifted_files"] == []
    assert payload["missing_files"] == []
    assert payload["secrets_included"] is False
    assert payload["writes_attempted"] == []


def test_agentmail_bridge_health_reports_drift_without_printing_secret_values(tmp_path):
    source_root = tmp_path / "source"
    runtime = tmp_path / "runtime"
    plist = tmp_path / "bridge.plist"
    files = (("agentmail-bridge/bridge.mjs", runtime / "bridge.mjs"),)
    _write(source_root / "agentmail-bridge/bridge.mjs", "token=source-secret")
    _write(runtime / "bridge.mjs", "token=runtime-secret")
    _write_plist(plist)

    with patch.object(health, "SOURCE_ROOT", source_root), patch.object(health, "SOURCE_BRIDGE_ROOT", source_root / "agentmail-bridge"), patch.object(health, "LAUNCHAGENT_PATH", plist), patch.object(health, "DEPLOYABLE_FILES", files):
        payload = health.build_agentmail_bridge_health(read_launchctl=False)

    rendered = json.dumps(payload)
    assert payload["status"] == "degraded"
    assert payload["drifted_files"] == ["agentmail-bridge/bridge.mjs"]
    assert "source-secret" not in rendered
    assert "runtime-secret" not in rendered


def test_agentmail_bridge_health_blocks_on_launchagent_mismatch(tmp_path):
    source_root = tmp_path / "source"
    runtime = tmp_path / "runtime"
    plist = tmp_path / "bridge.plist"
    files = (("agentmail-bridge/bridge.mjs", runtime / "bridge.mjs"),)
    _write(source_root / "agentmail-bridge/bridge.mjs", "same")
    _write(runtime / "bridge.mjs", "same")
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": health.LAUNCHAGENT_LABEL, "ProgramArguments": ["/tmp/wrong"], "WorkingDirectory": "/tmp"}))

    with patch.object(health, "SOURCE_ROOT", source_root), patch.object(health, "SOURCE_BRIDGE_ROOT", source_root / "agentmail-bridge"), patch.object(health, "LAUNCHAGENT_PATH", plist), patch.object(health, "DEPLOYABLE_FILES", files):
        payload = health.build_agentmail_bridge_health(read_launchctl=False)

    assert payload["status"] == "blocked"
    assert "launchagent_program_mismatch" in payload["launchagent"]["issues"]
