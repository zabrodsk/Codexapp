import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_command_ledger import TaskCommandLedger, command_fingerprint
from task_command_reconciler import reconcile_task_commands, recent_task_commands


def test_reconcile_reports_new_unprocessed_and_mark_missing_records_seen(tmp_path):
    db = tmp_path / "commands.sqlite3"
    discord_payload = {"status": "ok", "commands": [{"source": "Discord", "source_channel": "discord", "source_ref": "discord:c:m", "text": "Rocky remember to call Jana"}], "command_count": 1}

    payload = reconcile_task_commands(sources=["discord"], ledger_db_path=db, discord_payload=discord_payload)
    assert payload["results"][0]["status"] == "new_unprocessed"

    marked = reconcile_task_commands(sources=["discord"], ledger_db_path=db, discord_payload=discord_payload, mark_missing=True)
    assert marked["results"][0]["status"] == "new_unprocessed"
    assert marked["results"][0]["ledger_status"] == "seen"
    assert recent_task_commands(ledger_db_path=db)["command_count"] == 1


def test_reconcile_classifies_applied_duplicate_blocked_and_manual_review(tmp_path):
    db = tmp_path / "commands.sqlite3"
    ledger = TaskCommandLedger(db)
    commands = []
    for idx, status in enumerate(["applied", "skipped_duplicate", "blocked", "manual_review_required"]):
        cmd = {"source": "Command", "source_channel": "email", "source_ref": f"agentmail:{idx}", "text": f"Remember task {idx}"}
        commands.append(cmd)
        ledger.record_seen(cmd)
        ledger.update_outcome(source_ref=cmd["source_ref"], command_fingerprint=command_fingerprint(cmd), status=status, reason=status)
    email_payload = {"status": "ok", "commands": commands, "command_count": len(commands)}

    payload = reconcile_task_commands(sources=["email"], ledger_db_path=db, email_payload=email_payload)
    assert [row["status"] for row in payload["results"]] == ["applied", "skipped_duplicate", "blocked", "manual_review_required"]


def test_meeting_signals_are_reconciled_but_not_acknowledged(tmp_path):
    meeting_payload = {"status": "ok", "signals": [{"source_ref": "obsidian-meeting:1", "summary": "Weekly: Dusan - follow up", "observed_at": "2026-05-24T00:00:00+00:00", "meeting_title": "Weekly", "owner_hint": "Dusan"}], "signal_count": 1}

    payload = reconcile_task_commands(sources=["meeting"], ledger_db_path=tmp_path / "commands.sqlite3", meeting_payload=meeting_payload, mark_missing=True)

    assert payload["results"][0]["source_channel"] == "meeting"
    assert payload["results"][0]["status"] == "new_unprocessed"


def test_reconcile_reports_source_no_longer_visible_for_recent_ledger_rows(tmp_path):
    db = tmp_path / "commands.sqlite3"
    ledger = TaskCommandLedger(db)
    command = {"source": "Command", "source_channel": "email", "source_ref": "agentmail:old", "text": "Remember old task"}
    ledger.record_seen(command)

    payload = reconcile_task_commands(sources=["email"], ledger_db_path=db, email_payload={"status": "ok", "commands": [], "command_count": 0})

    assert payload["results"][0]["status"] == "source_no_longer_visible"
    assert payload["results"][0]["ledger_status"] == "seen"
