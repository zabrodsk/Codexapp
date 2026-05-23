import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from discord_task_command_reader import read_discord_task_commands


def test_discord_reader_accepts_only_dusan_explicit_commands(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "channels": {
                    "discord": {
                        "token": "discord-token",
                        "guilds": {"g1": {"channels": {"c1": {"requireMention": True}}}},
                    }
                },
                "agentmail": {"approverDiscordUserId": "u1"},
            }
        )
    )

    def fake_get(token, path):
        assert token == "discord-token"
        return [
            {"id": "1", "content": "ordinary chat", "timestamp": "2026-05-23T10:00:00+00:00", "author": {"id": "u1"}},
            {"id": "2", "content": "Rocky remember to call Jana", "timestamp": "2026-05-23T10:01:00+00:00", "author": {"id": "u1"}},
            {"id": "3", "content": "Rocky remember bot task", "timestamp": "2026-05-23T10:02:00+00:00", "author": {"id": "u1", "bot": True}},
            {"id": "4", "content": "Rocky remember outsider task", "timestamp": "2026-05-23T10:03:00+00:00", "author": {"id": "u2"}},
        ]

    payload = read_discord_task_commands(
        config_path=config,
        state_file=tmp_path / "state.json",
        http_get=fake_get,
        now=datetime(2026, 5, 23, 10, 5, tzinfo=timezone.utc),
    )

    assert payload["status"] == "ok"
    assert payload["command_count"] == 1
    assert payload["commands"][0]["source_ref"] == "discord:c1:2"
    assert "discord-token" not in json.dumps(payload)
