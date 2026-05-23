import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_repo_inspector import build_repo_signal_summary


def test_repo_signal_summary_reports_dirty_repos_without_diff(tmp_path):
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

    payload = build_repo_signal_summary(repo_roots=[repo])

    assert payload["status"] == "ok"
    assert payload["repo_count"] == 1
    assert payload["dirty_repo_count"] == 1
    assert payload["repos"][0]["dirty"] is True
    assert payload["calendar_write_attempted"] is False
    assert "print('two')" not in str(payload)
