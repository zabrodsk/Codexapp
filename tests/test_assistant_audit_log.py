import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_audit_log import AssistantAuditLog


def test_audit_log_records_required_fields_and_redacts_sensitive_values(tmp_path):
    ledger_path = tmp_path / "assistant_audit.jsonl"
    ledger = AssistantAuditLog(ledger_path)

    event = ledger.record_event(
        event_type="calendar.proposal_created",
        workflow="calendar_dry_run",
        idempotency_key="rocky:test:key",
        policy_version="test-policy",
        decision="created",
        reason="test",
        sources=[{"source_ref": "email:123", "email_body": "very private body"}],
        artifacts={"token": "secret-token", "safe": "ok"},
    )

    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    for key in (
        "audit_id",
        "event_type",
        "actor",
        "workflow",
        "created_at",
        "idempotency_key",
        "policy_version",
        "privacy_class",
        "decision",
        "reason",
        "sources",
        "artifacts",
    ):
        assert key in row
    assert row["audit_id"] == event.audit_id
    assert row["sources"][0]["email_body"]["redacted"] is True
    assert row["artifacts"]["token"]["redacted"] is True
    assert row["artifacts"]["safe"] == "ok"


def test_audit_log_is_append_only(tmp_path):
    ledger_path = tmp_path / "assistant_audit.jsonl"
    ledger = AssistantAuditLog(ledger_path)

    for reason in ("first", "second"):
        ledger.record_event(
            event_type="policy.allowed",
            workflow="calendar_policy_check",
            idempotency_key="rocky:test:key",
            policy_version="test-policy",
            decision="allowed",
            reason=reason,
        )

    rows = ledger_path.read_text().splitlines()
    assert len(rows) == 2
    assert json.loads(rows[0])["reason"] == "first"
    assert json.loads(rows[1])["reason"] == "second"


def test_recent_returns_newest_first(tmp_path):
    ledger = AssistantAuditLog(tmp_path / "assistant_audit.jsonl")
    first = ledger.record_event(
        event_type="policy.allowed",
        workflow="calendar_policy_check",
        idempotency_key="rocky:test:1",
        policy_version="test-policy",
        decision="allowed",
        reason="first",
    )
    second = ledger.record_event(
        event_type="policy.allowed",
        workflow="calendar_policy_check",
        idempotency_key="rocky:test:2",
        policy_version="test-policy",
        decision="allowed",
        reason="second",
    )

    recent = ledger.recent(limit=2)
    assert [event.audit_id for event in recent] == [second.audit_id, first.audit_id]


def test_scheduler_and_lock_events_are_allowed(tmp_path):
    ledger = AssistantAuditLog(tmp_path / "assistant_audit.jsonl")

    health = ledger.record_event(
        event_type="scheduler.health_ok",
        workflow="scheduler_health",
        idempotency_key="scheduler:test",
        policy_version="rocky-scheduler-policy-v1",
        decision="allowed",
        reason="ok",
    )
    duplicate = ledger.record_event(
        event_type="lock.duplicate_blocked",
        workflow="scheduler_health",
        idempotency_key="scheduler:test",
        policy_version="rocky-scheduler-policy-v1",
        decision="blocked",
        reason="active_lock_exists",
    )

    assert health.event_type == "scheduler.health_ok"
    assert duplicate.event_type == "lock.duplicate_blocked"


def test_scheduler_artifacts_are_redacted(tmp_path):
    ledger = AssistantAuditLog(tmp_path / "assistant_audit.jsonl")

    ledger.record_event(
        event_type="scheduler.health_blocked",
        workflow="scheduler_health",
        idempotency_key="scheduler:test",
        policy_version="rocky-scheduler-policy-v1",
        decision="blocked",
        reason="blocked",
        artifacts={"raw_log": "secret line", "safe": "ok"},
    )

    row = json.loads((tmp_path / "assistant_audit.jsonl").read_text().splitlines()[0])
    assert row["artifacts"]["raw_log"]["redacted"] is True
    assert row["artifacts"]["safe"] == "ok"


def test_calendar_status_events_are_allowed(tmp_path):
    ledger = AssistantAuditLog(tmp_path / "assistant_audit.jsonl")

    for event_type in (
        "calendar.state_reconciled",
        "calendar.state_marked_stale",
        "calendar.write_health_checked",
    ):
        event = ledger.record_event(
            event_type=event_type,
            workflow="calendar_status",
            idempotency_key=f"calendar:test:{event_type}",
            policy_version="rocky-calendar-policy-v1",
            decision="completed" if event_type == "calendar.state_reconciled" else "recovered",
            reason="test",
        )
        assert event.event_type == event_type
