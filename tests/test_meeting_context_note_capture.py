import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_context_note_capture import run_meeting_context_note_capture
from meeting_context_note_ledger import MeetingContextNoteLedger


def test_discord_context_note_is_recorded_and_acknowledged(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "channels": {
                    "discord": {
                        "token": "test-token",
                        "guilds": {"g": {"channels": {"123": {"requireMention": True}}}},
                    }
                },
                "agentmail": {"approverDiscordUserId": "u1"},
            }
        )
    )

    messages = [
        {
            "id": "m1",
            "content": "Rocky, for my meeting with Jana today, remember that runway is the key concern.",
            "timestamp": "2026-05-25T07:30:00+00:00",
            "author": {"id": "u1"},
        }
    ]

    def fake_get(token, path):
        return messages

    posted = []

    def fake_post(**kwargs):
        posted.append(kwargs)
        return {"status": "posted", "message_ids": ["ack1"]}

    payload = run_meeting_context_note_capture(
        live=True,
        config_path=config,
        state_file=tmp_path / "state.json",
        ledger_db_path=tmp_path / "notes.sqlite3",
        http_get=fake_get,
        post_func=fake_post,
        now=datetime(2026, 5, 25, 8, 0, tzinfo=timezone.utc),
    )

    assert payload["status"] == "ok"
    assert payload["notes_recorded"] == 1
    assert payload["ack_sent"] == 1
    assert "runway is the key concern" in posted[0]["content"]
    row = MeetingContextNoteLedger(tmp_path / "notes.sqlite3").recent(limit=1)[0]
    assert row["ack_status"] == "ack_sent"
    assert "token" not in str(row)


def test_dry_run_does_not_write_ledger(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"channels": {"discord": {"token": "t", "guilds": {"g": {"channels": {"123": {"requireMention": True}}}}}}, "agentmail": {"approverDiscordUserId": "u1"}}))

    payload = run_meeting_context_note_capture(
        live=False,
        config_path=config,
        state_file=tmp_path / "state.json",
        ledger_db_path=tmp_path / "notes.sqlite3",
        http_get=lambda token, path: [{"id": "m1", "content": "Rocky context for today: ask about runway.", "timestamp": "2026-05-25T07:30:00+00:00", "author": {"id": "u1"}}],
        now=datetime(2026, 5, 25, 8, 0, tzinfo=timezone.utc),
    )

    assert payload["notes_recorded"] == 0
    assert MeetingContextNoteLedger(tmp_path / "notes.sqlite3").recent(limit=1) == []
