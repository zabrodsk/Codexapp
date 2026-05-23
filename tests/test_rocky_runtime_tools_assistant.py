import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from rocky_runtime_tools import (
    build_parser,
    cmd_assistant_audit_recent,
    cmd_calendar_block_create,
    cmd_calendar_block_delete,
    cmd_calendar_block_reconcile,
    cmd_calendar_block_status,
    cmd_calendar_dry_run,
    cmd_calendar_policy_check,
    cmd_calendar_tcc_probe,
    cmd_calendar_write_health,
    cmd_email_triage_book,
    cmd_email_triage_proposals,
    cmd_email_triage_scheduler_run,
    cmd_training_calendar_proposals,
    cmd_training_calendar_book,
    cmd_training_calendar_reconcile,
    cmd_training_calendar_scheduler_run,
    cmd_assistant_notification_dispatch,
    cmd_trainingpeaks_ics_preview,
    cmd_trainingpeaks_read_path_check,
)


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def test_parser_includes_assistant_commands():
    parser = build_parser()

    assert parser.parse_args(["assistant-audit-recent"]).command == "assistant-audit-recent"
    assert parser.parse_args(
        [
            "calendar-policy-check",
            "--kind",
            "training",
            "--date",
            "2026-05-25",
            "--start",
            "08:00",
            "--duration-minutes",
            "90",
        ]
    ).command == "calendar-policy-check"
    assert parser.parse_args(
        [
            "calendar-dry-run",
            "--kind",
            "email_triage",
            "--date",
            "2026-05-25",
            "--window-start",
            "13:00",
            "--window-end",
            "14:00",
            "--duration-minutes",
            "30",
        ]
    ).command == "calendar-dry-run"
    assert parser.parse_args(
        [
            "calendar-block-create",
            "--kind",
            "task_focus",
            "--date",
            "2026-05-25",
            "--window-start",
            "07:00",
            "--window-end",
            "07:30",
            "--duration-minutes",
            "15",
        ]
    ).command == "calendar-block-create"
    assert parser.parse_args(
        [
            "calendar-block-delete",
            "--idempotency-key",
            "rocky:test",
        ]
    ).command == "calendar-block-delete"
    assert parser.parse_args(
        [
            "calendar-block-status",
            "--idempotency-key",
            "rocky:test",
        ]
    ).command == "calendar-block-status"
    assert parser.parse_args(["calendar-block-reconcile"]).command == "calendar-block-reconcile"
    assert parser.parse_args(["calendar-write-health"]).command == "calendar-write-health"
    assert parser.parse_args(["calendar-tcc-probe"]).command == "calendar-tcc-probe"
    assert parser.parse_args(["trainingpeaks-read-path-check"]).command == "trainingpeaks-read-path-check"
    assert parser.parse_args(
        ["trainingpeaks-ics-preview", "--ics-file", "/tmp/trainingpeaks.ics"]
    ).command == "trainingpeaks-ics-preview"
    assert parser.parse_args(["training-calendar-proposals"]).command == "training-calendar-proposals"
    assert parser.parse_args(
        ["training-calendar-book", "--idempotency-key", "rocky:training:test"]
    ).command == "training-calendar-book"
    assert parser.parse_args(["training-calendar-scheduler-run"]).command == "training-calendar-scheduler-run"
    assert parser.parse_args(["training-calendar-reconcile"]).command == "training-calendar-reconcile"
    assert parser.parse_args(["email-triage-proposals"]).command == "email-triage-proposals"
    assert parser.parse_args(
        ["email-triage-book", "--idempotency-key", "rocky:email:test"]
    ).command == "email-triage-book"
    assert parser.parse_args(["email-triage-scheduler-run"]).command == "email-triage-scheduler-run"
    assert parser.parse_args(
        ["assistant-notification-dispatch", "--status", "blocked", "--reason", "test", "--dry-run"]
    ).command == "assistant-notification-dispatch"


def test_calendar_policy_check_json_outputs_audit_id(tmp_path, capsys):
    result = cmd_calendar_policy_check(
        _args(
            kind="training",
            date="2026-05-25",
            start="08:00",
            duration_minutes=90,
            label="Endurance",
            source_ref=["trainingpeaks:test"],
            ledger_path=str(tmp_path / "assistant_audit.jsonl"),
            json_output=True,
        )
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["status"] == "allowed"
    assert payload["audit_id"]
    assert payload["calendar_write_attempted"] is False


def test_calendar_policy_check_returns_1_for_weekend(tmp_path, capsys):
    result = cmd_calendar_policy_check(
        _args(
            kind="email_triage",
            date="2026-05-24",
            start="13:00",
            duration_minutes=30,
            label=None,
            source_ref=[],
            ledger_path=str(tmp_path / "assistant_audit.jsonl"),
            json_output=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "blocked"
    assert payload["calendar_write_attempted"] is False


def test_calendar_dry_run_uses_read_only_proposal_path(tmp_path, capsys):
    with patch("assistant_calendar_dry_run.query_events", return_value=[]):
        result = cmd_calendar_dry_run(
            _args(
                kind="email_triage",
                date="2026-05-25",
                window_start="13:00",
                window_end="15:00",
                duration_minutes=30,
                label=None,
                reason="test",
                confidence="medium",
                source_ref=["mail:test"],
                db_path=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "proposal"
    assert payload["audit_id"]
    assert payload["calendar_write_attempted"] is False


def test_assistant_audit_recent_json_outputs_events(tmp_path, capsys):
    ledger_path = tmp_path / "assistant_audit.jsonl"
    cmd_calendar_policy_check(
        _args(
            kind="training",
            date="2026-05-25",
            start="08:00",
            duration_minutes=90,
            label=None,
            source_ref=[],
            ledger_path=str(ledger_path),
            json_output=True,
        )
    )
    capsys.readouterr()

    result = cmd_assistant_audit_recent(
        _args(limit=10, ledger_path=str(ledger_path), json_output=True)
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert len(payload) == 1
    assert payload[0]["event_type"] == "policy.allowed"


def test_calendar_block_create_refuses_without_live(tmp_path, capsys):
    result = cmd_calendar_block_create(
        _args(
            kind="task_focus",
            date="2026-05-25",
            window_start="07:00",
            window_end="07:30",
            duration_minutes=15,
            label="Sprint 3 smoke",
            reason="test",
            confidence="medium",
            source_ref=["test:sprint3"],
            calendar_name="Calendar",
            db_path=None,
            state_db=str(tmp_path / "assistant_calendar.sqlite3"),
            scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
            ledger_path=str(tmp_path / "assistant_audit.jsonl"),
            live=False,
            json_output=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False


def test_calendar_block_delete_refuses_without_live(tmp_path, capsys):
    result = cmd_calendar_block_delete(
        _args(
            idempotency_key="rocky:test",
            calendar_name="Calendar",
            db_path=None,
            state_db=str(tmp_path / "assistant_calendar.sqlite3"),
            scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
            ledger_path=str(tmp_path / "assistant_audit.jsonl"),
            live=False,
            json_output=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "blocked"
    assert payload["reason"] == "live_flag_required"
    assert payload["calendar_write_attempted"] is False


def test_calendar_block_status_json_uses_inspection_path(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.inspect_calendar_block",
        return_value={
            "status": "deleted_verified",
            "idempotency_key": "rocky:test",
            "calendar_match_count": 0,
            "calendar_matches": [],
            "recommended_action": "No action needed.",
        },
    ):
        result = cmd_calendar_block_status(
            _args(
                idempotency_key="rocky:test",
                calendar_name="Calendar",
                db_path=None,
                state_db=str(tmp_path / "assistant_calendar.sqlite3"),
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "deleted_verified"


def test_calendar_block_reconcile_json_can_mark_stale(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.reconcile_calendar_blocks",
        return_value={
            "status": "ok",
            "calendar_name": "Calendar",
            "checked_count": 1,
            "marked_stale_count": 1,
            "mark_stale": True,
            "blocks": [],
        },
    ) as reconcile:
        result = cmd_calendar_block_reconcile(
            _args(
                calendar_name="Calendar",
                db_path=None,
                state_db=str(tmp_path / "assistant_calendar.sqlite3"),
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                mark_stale=True,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["marked_stale_count"] == 1
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs["mark_stale"] is True


def test_calendar_write_health_json_reports_status(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.calendar_write_health",
        return_value={
            "status": "ok",
            "blocked_checks": [],
            "checks": {},
            "calendar_write_attempted": False,
        },
    ):
        result = cmd_calendar_write_health(
            _args(
                db_path=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                write_audit=True,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "ok"
    assert payload["calendar_write_attempted"] is False


def test_trainingpeaks_read_path_check_json_uses_read_only_probe(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.probe_trainingpeaks_read_paths",
        return_value={
            "status": "blocked",
            "recommendation": {"recommended_path": None, "decision": "blocked_missing_trainingpeaks_source"},
            "calendar_write_attempted": False,
        },
    ) as probe:
        result = cmd_trainingpeaks_read_path_check(
            _args(
                calendar_db_path=None,
                events_db_path=str(tmp_path / "events.db"),
                webcal_url_file=str(tmp_path / "missing-url"),
                start_date="2026-05-25",
                days_ahead=14,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["calendar_write_attempted"] is False
    probe.assert_called_once()


def test_trainingpeaks_ics_preview_json_uses_ics_file_reader(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.preview_ics_file",
        return_value={
            "status": "ok",
            "workout_count": 1,
            "workouts": [],
            "calendar_write_attempted": False,
        },
    ) as preview:
        result = cmd_trainingpeaks_ics_preview(
            _args(
                ics_file=str(tmp_path / "trainingpeaks.ics"),
                webcal_url_file=None,
                start_date="2026-05-25",
                days_ahead=14,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["calendar_write_attempted"] is False
    preview.assert_called_once()


def test_trainingpeaks_ics_preview_json_uses_secret_webcal_file(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.preview_webcal_url_file",
        return_value={
            "status": "ok",
            "workout_count": 0,
            "workouts": [],
            "input": {"url_redacted": True},
            "calendar_write_attempted": False,
        },
    ) as preview:
        result = cmd_trainingpeaks_ics_preview(
            _args(
                ics_file=None,
                webcal_url_file=str(tmp_path / "trainingpeaks-webcal-url"),
                start_date="2026-05-25",
                days_ahead=14,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["input"]["url_redacted"] is True
    preview.assert_called_once()


def test_training_calendar_proposals_json_uses_dry_run_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.build_training_calendar_proposals",
        return_value={
            "status": "proposal",
            "mode": "dry_run",
            "target_date": "2026-05-27",
            "selected_workout_count": 1,
            "proposals": [],
            "calendar_write_attempted": False,
        },
    ) as proposals:
        result = cmd_training_calendar_proposals(
            _args(
                webcal_url_file=str(tmp_path / "trainingpeaks-webcal-url"),
                planning_date="2026-05-23",
                target_working_days=3,
                days_ahead=14,
                db_path=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                write_audit=False,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["calendar_write_attempted"] is False
    proposals.assert_called_once()
    assert proposals.call_args.kwargs["write_audit"] is False


def test_training_calendar_book_json_uses_live_booking_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.book_training_calendar_proposal",
        return_value={
            "status": "created",
            "reason": None,
            "idempotency_key": "rocky:training:2026-05-27:test",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as book:
        result = cmd_training_calendar_book(
            _args(
                idempotency_key="rocky:training:2026-05-27:test",
                webcal_url_file=str(tmp_path / "trainingpeaks-webcal-url"),
                planning_date="2026-05-23",
                target_working_days=3,
                days_ahead=14,
                calendar_name="Calendar",
                live=True,
                db_path=None,
                state_db=None,
                scheduler_db=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["calendar_write_attempted"] is True
    book.assert_called_once()
    assert book.call_args.kwargs["idempotency_key"] == "rocky:training:2026-05-27:test"
    assert book.call_args.kwargs["live"] is True


def test_training_calendar_scheduler_json_uses_scheduler_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.run_training_calendar_scheduler",
        return_value={
            "status": "skipped_duplicate",
            "reason": "duplicate_existing_active_event",
            "target_date": "2026-05-27",
            "calendar_write_attempted": False,
        },
    ) as scheduler:
        result = cmd_training_calendar_scheduler_run(
            _args(
                webcal_url_file=str(tmp_path / "trainingpeaks-webcal-url"),
                planning_date="2026-05-23",
                target_working_days=3,
                days_ahead=14,
                calendar_name="Calendar",
                max_bookings=1,
                live=True,
                reconcile=True,
                fix_safe=True,
                notify_failures=True,
                notification_dry_run=True,
                notification_channel_id="channel",
                db_path=None,
                calendar_state_db=None,
                scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                state_file=str(tmp_path / "training_calendar_scheduler.json"),
                lock_ttl_seconds=1800,
                write_audit=True,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "skipped_duplicate"
    scheduler.assert_called_once()
    assert scheduler.call_args.kwargs["live"] is True
    assert scheduler.call_args.kwargs["reconcile"] is True
    assert scheduler.call_args.kwargs["notify_failures"] is True


def test_email_triage_proposals_json_uses_dry_run_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.build_email_triage_proposals",
        return_value={
            "status": "blocked",
            "reason": "proactive_booking_blocked_on_friday_saturday_sunday",
            "target_date": "2026-05-23",
            "calendar_write_attempted": False,
            "proposals": [],
        },
    ) as proposals:
        result = cmd_email_triage_proposals(
            _args(
                planning_date="2026-05-23",
                hours=168,
                limit=100,
                db_path=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                write_audit=False,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["calendar_write_attempted"] is False
    proposals.assert_called_once()
    assert proposals.call_args.kwargs["write_audit"] is False


def test_email_triage_book_json_uses_live_booking_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.book_email_triage_proposal",
        return_value={
            "status": "created",
            "reason": None,
            "idempotency_key": "rocky:email:2026-05-25:test",
            "calendar_write_attempted": True,
            "calendar_event_created": True,
            "calendar_event_deleted": False,
        },
    ) as book:
        result = cmd_email_triage_book(
            _args(
                idempotency_key="rocky:email:2026-05-25:test",
                planning_date="2026-05-25",
                calendar_name="Calendar",
                live=True,
                hours=168,
                limit=100,
                db_path=None,
                state_db=None,
                scheduler_db=None,
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["calendar_write_attempted"] is True
    book.assert_called_once()
    assert book.call_args.kwargs["idempotency_key"] == "rocky:email:2026-05-25:test"
    assert book.call_args.kwargs["live"] is True


def test_email_triage_scheduler_json_uses_scheduler_engine(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.run_email_triage_scheduler",
        return_value={
            "status": "skipped_weekend_target",
            "reason": "proactive_booking_blocked_on_friday_saturday_sunday",
            "target_date": "2026-05-23",
            "calendar_write_attempted": False,
        },
    ) as scheduler:
        result = cmd_email_triage_scheduler_run(
            _args(
                planning_date="2026-05-23",
                calendar_name="Calendar",
                live=True,
                notify_failures=True,
                notification_dry_run=True,
                notification_channel_id="channel",
                hours=168,
                limit=100,
                db_path=None,
                calendar_state_db=None,
                scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                state_file=str(tmp_path / "email_triage_scheduler.json"),
                lock_ttl_seconds=1800,
                write_audit=True,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "skipped_weekend_target"
    scheduler.assert_called_once()
    assert scheduler.call_args.kwargs["live"] is True
    assert scheduler.call_args.kwargs["notify_failures"] is True


def test_calendar_tcc_probe_json_uses_probe_engine(capsys):
    with patch(
        "rocky_runtime_tools.build_calendar_tcc_probe",
        return_value={
            "status": "blocked",
            "failure_class": "calendar_tcc_blocked",
            "calendar_write_attempted": False,
        },
    ) as probe:
        result = cmd_calendar_tcc_probe(
            _args(
                db_path=None,
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["failure_class"] == "calendar_tcc_blocked"
    assert payload["calendar_write_attempted"] is False
    probe.assert_called_once()


def test_runtime_deployable_files_include_training_calendar_modules():
    from rocky_runtime_tools import RUNTIME_DEPLOYABLE_FILES

    assert "scripts/training_calendar_proposal_engine.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/training_calendar_live_booking.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/training_calendar_scheduler.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/training_calendar_reconciler.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/assistant_notification_dispatcher.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/assistant_calendar_tcc_probe.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/email_triage_proposal_engine.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/email_triage_live_booking.py" in RUNTIME_DEPLOYABLE_FILES
    assert "scripts/email_triage_scheduler.py" in RUNTIME_DEPLOYABLE_FILES


def test_training_calendar_reconcile_cli_uses_reconcile_path(tmp_path, capsys):
    with patch(
        "rocky_runtime_tools.reconcile_training_calendar",
        return_value={
            "status": "ok",
            "reason": "training_calendar_reconciled",
            "calendar_write_attempted": False,
        },
    ) as reconcile:
        result = cmd_training_calendar_reconcile(
            _args(
                webcal_url_file="/tmp/webcal-secret",
                planning_date="2026-05-23",
                days_ahead=14,
                calendar_name="Calendar",
                fix_safe=True,
                live=True,
                notify_failures=True,
                notification_dry_run=True,
                notification_channel_id="channel",
                db_path=None,
                state_db=str(tmp_path / "assistant_calendar.sqlite3"),
                scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
                ledger_path=str(tmp_path / "assistant_audit.jsonl"),
                json_output=True,
            )
        )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "ok"
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs["fix_safe"] is True
    assert reconcile.call_args.kwargs["live"] is True


def test_assistant_notification_dispatch_cli_dry_run(tmp_path, capsys):
    result = cmd_assistant_notification_dispatch(
        _args(
            status="blocked",
            reason="manual_review_required",
            target_date="2026-05-27",
            idempotency_key="rocky:training:test",
            channel_id="channel",
            config_path=str(tmp_path / "openclaw.json"),
            ledger_path=str(tmp_path / "assistant_audit.jsonl"),
            scheduler_db=str(tmp_path / "assistant_scheduler.sqlite3"),
            dry_run=True,
            json_output=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "dry_run"
    assert payload["notification_attempted"] is False
