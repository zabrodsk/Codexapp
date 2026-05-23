import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_notification_dispatcher import (
    DEFAULT_ALERT_CHANNEL_ID,
    dispatch_failure_notification,
    should_notify,
)
from assistant_scheduler_state import AssistantSchedulerState


def test_should_notify_only_attention_needed_statuses():
    assert should_notify({"status": "blocked"}) is True
    assert should_notify({"status": "manual_review_required"}) is True
    assert should_notify({"status": "source_ref_drift_verified"}) is False
    assert should_notify({"status": "skipped_duplicate"}) is False


def test_notification_dry_run_redacts_sensitive_payload(tmp_path):
    payload = dispatch_failure_notification(
        {
            "status": "blocked",
            "reason": "manual_review_required",
            "webcal": "webcal://secret-token-value",
            "description": "coach notes should not appear",
        },
        dry_run=True,
        ledger_path=tmp_path / "assistant_audit.jsonl",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
    )

    rendered = json.dumps(payload)
    assert payload["status"] == "dry_run"
    assert payload["channel_id"] == DEFAULT_ALERT_CHANNEL_ID
    assert "secret-token-value" not in rendered
    assert "coach notes should not appear" not in rendered
    assert payload["notification_attempted"] is False


def test_notification_success_audits_without_logging_token(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"channels": {"discord": {"token": "super-secret-token"}}}))
    calls = []

    def poster(*, token, channel_id, content):
        calls.append((token, channel_id, content))
        return {"status": "posted", "message_ids": ["m1"], "channel_id": channel_id}

    payload = dispatch_failure_notification(
        {"status": "blocked", "reason": "calendar_write_health_not_ok"},
        config_path=config,
        ledger_path=tmp_path / "assistant_audit.jsonl",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        post_func=poster,
    )

    assert payload["status"] == "posted"
    assert calls[0][0] == "super-secret-token"
    rendered = (tmp_path / "assistant_audit.jsonl").read_text()
    assert "super-secret-token" not in rendered
    assert "assistant.notification_sent" in rendered


def test_notification_failure_creates_dead_letter(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"channels": {"discord": {"token": "super-secret-token"}}}))

    def poster(*, token, channel_id, content):
        return {"status": "failed", "reason": "discord_500"}

    payload = dispatch_failure_notification(
        {"status": "blocked", "reason": "manual_review_required"},
        config_path=config,
        ledger_path=tmp_path / "assistant_audit.jsonl",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        post_func=poster,
    )

    assert payload["status"] == "failed"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert dead[-1]["failure_class"] == "assistant_notification_failed"
    assert "super-secret-token" not in json.dumps(dead)
