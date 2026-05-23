import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_dry_run import build_calendar_dry_run


def _event(summary, start, end, description=""):
    return {
        "summary": summary,
        "start_local": start,
        "end_local": end,
        "all_day": False,
        "calendar": "Work",
        "location": "",
        "description": description,
    }


def test_dry_run_creates_proposal_without_calendar_write(tmp_path):
    payload = build_calendar_dry_run(
        kind="email_triage",
        day="2026-05-25",
        window_start="13:00",
        window_end="15:00",
        duration_minutes=30,
        source_refs=["mail:unread-attention"],
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "proposal"
    assert payload["calendar_write_attempted"] is False
    assert payload["audit_id"]
    assert payload["idempotency_key"]
    assert payload["title"] == "Rocky: Email triage - unread attention"
    for expected in (
        "Booked by: Rocky",
        "Block type: email_triage",
        "Audit ID:",
        "Idempotency key:",
        "Reversal instruction:",
    ):
        assert expected in payload["metadata_description"]


def test_dry_run_blocks_friday_before_calendar_lookup(tmp_path):
    payload = build_calendar_dry_run(
        kind="training",
        day="2026-05-22",
        window_start="08:00",
        window_end="10:00",
        duration_minutes=90,
        existing_events=[],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "policy_blocked"
    assert payload["calendar_write_attempted"] is False
    assert "proactive_booking_blocked_on_friday_saturday_sunday" in payload["policy_decision"]["reasons"]


def test_dry_run_blocks_when_no_available_slot(tmp_path):
    payload = build_calendar_dry_run(
        kind="email_triage",
        day="2026-05-25",
        window_start="13:00",
        window_end="14:00",
        duration_minutes=60,
        existing_events=[
            _event("Existing meeting", "2026-05-25 13:00:00", "2026-05-25 14:00:00")
        ],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "no_available_slot"
    assert payload["calendar_write_attempted"] is False
    assert payload["conflict_count"] == 1


def test_dry_run_blocks_duplicate_rocky_block(tmp_path):
    payload = build_calendar_dry_run(
        kind="coding_focus",
        day="2026-05-25",
        window_start="13:00",
        window_end="16:00",
        duration_minutes=60,
        label="OpenClaw",
        existing_events=[
            _event(
                "Rocky: Coding focus - OpenClaw",
                "2026-05-25 13:30:00",
                "2026-05-25 14:30:00",
            )
        ],
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "blocked"
    assert payload["reason"] == "duplicate_rocky_block"
    assert payload["calendar_write_attempted"] is False
    assert payload["duplicate_count"] == 1
