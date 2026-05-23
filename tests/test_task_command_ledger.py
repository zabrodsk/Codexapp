import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_command_ledger import TaskCommandLedger, command_fingerprint


def test_ledger_records_seen_applied_and_ack_sent_without_raw_secret(tmp_path):
    ledger = TaskCommandLedger(tmp_path / "commands.sqlite3")
    command = {"source": "Discord", "source_channel": "discord", "source_ref": "discord:c:m", "text": "Remember token=secret to call Jana", "channel_id": "c"}
    row = ledger.record_seen(command)
    assert row["status"] == "seen"
    assert row["text_preview"].startswith("[redacted:")

    fp = command_fingerprint(command)
    row = ledger.update_outcome(source_ref="discord:c:m", command_fingerprint=fp, status="applied", reason="created", task={"title": "Call Jana", "page_id": "page1"}, audit_id="audit1")
    assert row["status"] == "applied"
    assert row["task_page_id"] == "page1"

    row = ledger.update_ack(source_ref="discord:c:m", command_fingerprint=fp, status="ack_sent", message_id="msg1")
    assert row["status"] == "ack_sent"
    assert row["ack_message_id"] == "msg1"
    assert "secret" not in json.dumps(row)


def test_ledger_recent_counts_by_status_and_source(tmp_path):
    ledger = TaskCommandLedger(tmp_path / "commands.sqlite3")
    ledger.record_seen({"source": "Command", "source_channel": "email", "source_ref": "agentmail:m1", "text": "Remember to call Jana"})
    ledger.record_seen({"source": "Meeting", "source_channel": "meeting", "source_ref": "meeting:1", "text": "Dusan - follow up"})

    assert ledger.counts_by_status()["seen"] == 2
    assert ledger.counts_by_source() == {"email": 1, "meeting": 1}
    assert len(ledger.recent(limit=1)) == 1
