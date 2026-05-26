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
    render_notification,
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


def test_discord_403_falls_back_to_agentmail(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"channels": {"discord": {"token": "super-secret-token"}}}))
    emails = []

    def poster(*, token, channel_id, content):
        return {"status": "failed", "reason": "discord_http_403", "status_code": 403}

    def emailer(*, to_email, subject, text, config_path, credentials_path):
        emails.append({"to": to_email, "subject": subject, "text": text})
        return {"status": "posted", "message_id": "email-1", "to": to_email}

    payload = dispatch_failure_notification(
        {"status": "blocked", "reason": "manual_review_required"},
        config_path=config,
        ledger_path=tmp_path / "assistant_audit.jsonl",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        post_func=poster,
        agentmail_send_func=emailer,
    )

    assert payload["status"] == "posted"
    assert payload["final_status"] == "posted"
    assert payload["fallback_used"] is True
    assert payload["primary_failure_reason"] == "discord_permission_denied"
    assert emails and emails[0]["to"] == "dusan.zabrodsky@rockaway.cz"
    assert "Fallback email from Rocky" in emails[0]["text"]
    assert "Primary Discord delivery failed: discord_permission_denied" in emails[0]["text"]
    assert "email only because the primary Discord route is unavailable" in emails[0]["text"]
    assert AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters() == []


def test_both_notification_channels_failing_creates_one_dead_letter(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({"channels": {"discord": {"token": "super-secret-token"}}}))

    def poster(*, token, channel_id, content):
        return {"status": "failed", "reason": "discord_http_403", "status_code": 403}

    def emailer(*, to_email, subject, text, config_path, credentials_path):
        return {"status": "failed", "reason": "agentmail_outbound_failed"}

    payload = dispatch_failure_notification(
        {"status": "blocked", "reason": "manual_review_required"},
        config_path=config,
        ledger_path=tmp_path / "assistant_audit.jsonl",
        scheduler_db_path=tmp_path / "assistant_scheduler.sqlite3",
        post_func=poster,
        agentmail_send_func=emailer,
    )

    assert payload["status"] == "failed"
    assert payload["fallback_used"] is True
    assert payload["primary_failure_reason"] == "discord_permission_denied"
    dead = AssistantSchedulerState(tmp_path / "assistant_scheduler.sqlite3").list_dead_letters()
    assert len(dead) == 1
    assert dead[0]["failure_class"] == "assistant_notification_failed"


def test_notification_title_uses_workflow_not_training_specific_text():
    message = render_notification(
        {"workflow": "email_triage_scheduler", "status": "blocked", "reason": "calendar_write_health_not_ok"}
    )

    assert message.splitlines()[0] == "Rocky email triage scheduler needs attention"
    assert "training calendar" not in message
