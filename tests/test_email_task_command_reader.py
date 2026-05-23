import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from email_task_command_reader import read_email_task_commands


def test_email_command_reader_reads_local_mirrors_without_raw_secret(tmp_path):
    inbox = tmp_path / "email"
    inbox.mkdir()
    (inbox / "commands.jsonl").write_text(
        json.dumps(
            {
                "created_at": "2026-05-23T10:00:00+00:00",
                "sender": "dusan@example.com",
                "message_id": "m1",
                "thread_id": "t1",
                "text": "Remember to follow up with Jana",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = read_email_task_commands(inbox_dir=inbox, now=datetime(2026, 5, 23, 10, 5, tzinfo=timezone.utc))

    assert payload["status"] == "ok"
    assert payload["command_count"] == 1
    assert payload["commands"][0]["source_ref"].startswith("agentmail:")
    assert "dusan@example.com" not in json.dumps(payload)
