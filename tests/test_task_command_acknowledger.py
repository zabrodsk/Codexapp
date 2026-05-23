import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_command_acknowledger import maybe_acknowledge_discord_command
from task_command_ledger import TaskCommandLedger, command_fingerprint


def test_discord_ack_posts_once_and_records_ack_sent(tmp_path):
    ledger = TaskCommandLedger(tmp_path / "commands.sqlite3")
    command = {"source": "Discord", "source_channel": "discord", "source_ref": "discord:c:m", "text": "Rocky remember to call Jana", "channel_id": "c"}
    ledger.record_seen(command)
    ledger.update_outcome(source_ref="discord:c:m", command_fingerprint=command_fingerprint(command), status="applied", task={"title": "Call Jana"})
    posted = []

    def post_func(**kwargs):
        posted.append(kwargs)
        return {"status": "posted", "channel_id": kwargs["channel_id"], "message_ids": ["ack1"]}

    config = tmp_path / "openclaw.json"
    config.write_text('{"channels":{"discord":{"token":"discord-token"}}}')
    payload = maybe_acknowledge_discord_command(command, {"status": "created", "task": {"title": "Call Jana"}}, ledger=ledger, config_path=config, post_func=post_func)
    again = maybe_acknowledge_discord_command(command, {"status": "created", "task": {"title": "Call Jana"}}, ledger=ledger, config_path=config, post_func=post_func)

    assert payload["status"] == "ack_sent"
    assert again["reason"] == "ack_already_sent"
    assert len(posted) == 1
    assert posted[0]["content"] == "Got it. Added task: Call Jana"


def test_ack_failure_records_ack_failed_without_rolling_back_task(tmp_path):
    ledger = TaskCommandLedger(tmp_path / "commands.sqlite3")
    command = {"source": "Discord", "source_channel": "discord", "source_ref": "discord:c:m", "text": "Rocky remember to call Jana", "channel_id": "c"}
    ledger.record_seen(command)
    fp = command_fingerprint(command)
    ledger.update_outcome(source_ref="discord:c:m", command_fingerprint=fp, status="applied", task={"title": "Call Jana"})
    config = tmp_path / "openclaw.json"
    config.write_text('{"channels":{"discord":{"token":"discord-token"}}}')

    payload = maybe_acknowledge_discord_command(command, {"status": "created", "task": {"title": "Call Jana"}}, ledger=ledger, config_path=config, post_func=lambda **_: {"status": "failed", "reason": "discord_http_500"})
    row = ledger.get(source_ref="discord:c:m", command_fingerprint=fp)

    assert payload["status"] == "ack_failed"
    assert row["status"] == "ack_failed"
    assert row["task_title"] == "Call Jana"


def test_email_and_meeting_acknowledgements_are_skipped(tmp_path):
    ledger = TaskCommandLedger(tmp_path / "commands.sqlite3")
    command = {"source": "Command", "source_channel": "email", "source_ref": "agentmail:m", "text": "Remember to call Jana"}
    assert maybe_acknowledge_discord_command(command, {"status": "created"}, ledger=ledger)["reason"] == "ack_not_supported_for_source"
