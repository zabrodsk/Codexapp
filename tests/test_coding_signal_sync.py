import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_signal_sync import build_coding_signal_manifest, inspect_codex_sessions, inspect_claude_sessions, inspect_repositories, write_manifest


def test_codex_and_claude_sessions_are_sanitized(tmp_path):
    codex_root = tmp_path / "codex" / "sessions"
    codex_root.mkdir(parents=True)
    codex_file = codex_root / "rollout-2026-05-23T10-00-00-session-1.jsonl"
    codex_file.write_text(
        '\n'.join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "session-1", "cwd": "/repo/app", "timestamp": "2026-05-23T08:00:00Z"}}),
                json.dumps({"type": "turn_context", "payload": {"cwd": "/repo/app", "summary": "raw transcript should not be copied"}}),
                json.dumps({"type": "response_item", "payload": {"type": "message", "content": "secret token abc"}}),
            ]
        )
        + '\n'
    )
    index = tmp_path / "session_index.jsonl"
    index.write_text(json.dumps({"id": "session-1", "thread_name": "Finish checkout flow", "updated_at": "2026-05-23T09:00:00Z"}) + "\n")
    claude_root = tmp_path / "claude" / "projects"
    claude_root.mkdir(parents=True)
    (claude_root / "abc.jsonl").write_text(json.dumps({"sessionId": "abc", "cwd": "/repo/api", "gitBranch": "main", "timestamp": "2026-05-23T09:00:00Z", "message": {"content": "raw"}}) + "\n")

    codex = inspect_codex_sessions(sessions_root=codex_root, index_path=index)
    claude = inspect_claude_sessions(projects_root=claude_root)

    assert codex[0]["thread_name"] == "Finish checkout flow"
    assert "raw transcript" not in json.dumps(codex)
    assert claude[0]["git_branch"] == "main"
    assert claude[0]["message_count"] == 1
    assert "raw" not in json.dumps(claude)


def test_repo_inspection_reports_dirty_counts_without_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("print('one')\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test User", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "app.py").write_text("print('two')\n")

    payload = inspect_repositories(repo_roots=[repo])

    assert payload[0]["dirty"] is True
    assert payload[0]["modified_count"] == 1
    assert "print('two')" not in json.dumps(payload)


def test_manifest_write_redacts_auth_like_strings(tmp_path):
    manifest = build_coding_signal_manifest(codex_sessions_root=tmp_path / "none", claude_projects_root=tmp_path / "none2", repo_roots=[])
    manifest["codex_sessions"].append({"thread_name": "contains token abc", "source_ref": "codex:session:test"})
    out = tmp_path / "latest.json"

    write_manifest(out, manifest)
    text = out.read_text()

    assert "token abc" not in text
    assert "[redacted:" in text
