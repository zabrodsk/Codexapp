import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_session_inspector import inspect_coding_signals


def test_inspector_loads_laptop_manifest_and_builds_signals(tmp_path):
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_at": "2099-05-23T09:00:00+00:00",
                "codex_sessions": [{"session_id": "s1", "source_ref": "codex:session:s1", "project": "Matchbook", "thread_name": "Finish deploy", "updated_at": "2026-05-23T09:00:00Z"}],
                "claude_sessions": [],
                "repos": [{"source_ref": "git:repo:1", "project": "Matchbook", "path": "/repo", "branch": "main", "dirty": True, "modified_count": 2}],
            }
        )
    )

    payload = inspect_coding_signals(laptop_manifest_path=manifest, include_local_sessions=False, include_repos=False)

    assert payload["status"] == "ok"
    assert payload["signal_count"] == 2
    assert payload["signals"][0]["where_left_off"]
    assert "raw" not in json.dumps(payload).lower()


def test_inspector_none_manifest_path_uses_default_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_at": "2099-05-23T09:00:00+00:00",
                "codex_sessions": [{"session_id": "s1", "source_ref": "codex:session:s1", "project": "private-local-memory-os", "thread_name": "Rocky propagation", "updated_at": "2026-05-23T09:00:00Z"}],
                "claude_sessions": [],
                "repos": [],
            }
        )
    )
    monkeypatch.setattr("coding_session_inspector.DEFAULT_LAPTOP_MANIFEST_PATH", manifest)

    payload = inspect_coding_signals(laptop_manifest_path=None, include_local_sessions=False, include_repos=False)

    assert payload["laptop_manifest_status"]["status"] == "ok"
    assert payload["signals"][0]["origin"] == "laptop_manifest"
