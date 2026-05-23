import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from coding_focus_proposal_engine import build_coding_focus_proposals


def _item(**overrides):
    item = {
        "work_item_id": "coding-work:1",
        "project": "Matchbook",
        "title": "Finish go-live smoke",
        "status": "active",
        "priority": "High",
        "estimated_effort_minutes": 90,
        "confidence": 0.86,
        "requires_dusan_decision": False,
        "source_refs": ["codex:session:1", "git:repo:1"],
        "where_left_off": "Tests passed; production smoke remains",
        "recommended_next_step": "Run smoke, inspect logs, and write handoff",
        "done_signal": "Production smoke passes and handoff is posted",
    }
    item.update(overrides)
    return item


def test_coding_focus_proposal_description_contains_focus_context():
    payload = build_coding_focus_proposals(
        planning_date="2026-05-25",
        work_items=[_item()],
        existing_events=[],
        write_audit=False,
    )

    proposal = payload["proposals"][0]
    description = proposal["metadata_description"]
    assert payload["status"] == "proposal"
    assert proposal["idempotency_key"].startswith("rocky:coding_focus:2026-05-25:")
    assert "Focus for this block" in description
    assert "Where you left off" in description
    assert "Tests passed; production smoke remains" in description
    assert "Recommended next step" in description
    assert "Run smoke" in description
    assert "Idempotency key" in description


def test_coding_focus_proposal_redacts_sensitive_description_text():
    payload = build_coding_focus_proposals(
        planning_date="2026-05-25",
        work_items=[_item(where_left_off="Use token abc to continue", recommended_next_step="Paste cookie secret")],
        existing_events=[],
        write_audit=False,
    )

    description = payload["proposals"][0]["metadata_description"]
    assert "token abc" not in description
    assert "cookie secret" not in description
    assert "[redacted]" in description


def test_coding_focus_blocks_weekend_and_short_or_late_policy():
    weekend = build_coding_focus_proposals(planning_date="2026-05-23", work_items=[_item()], existing_events=[], write_audit=False)
    assert weekend["status"] == "skipped_weekend_target"

    low_conf = build_coding_focus_proposals(planning_date="2026-05-25", work_items=[_item(confidence=0.5)], existing_events=[], write_audit=False)
    assert low_conf["status"] == "skipped_no_coding_focus"


def test_existing_coding_focus_block_counts_as_duplicate():
    payload = build_coding_focus_proposals(
        planning_date="2026-05-25",
        work_items=[_item()],
        existing_events=[{"summary": "Rocky: Coding focus - Matchbook", "description": "Booked by: Rocky", "start_local": "2026-05-25 12:30:00", "end_local": "2026-05-25 14:00:00", "all_day": False, "calendar": "Calendar"}],
        write_audit=False,
    )

    assert payload["status"] == "blocked"
    assert payload["proposals"][0]["reason"] == "duplicate_rocky_block"


def test_calendar_conflict_blocks_when_no_available_slot():
    payload = build_coding_focus_proposals(
        planning_date="2026-05-25",
        work_items=[_item()],
        existing_events=[{"summary": "Busy", "description": "", "start_local": "2026-05-25 12:30:00", "end_local": "2026-05-25 19:30:00", "all_day": False, "calendar": "Calendar"}],
        write_audit=False,
    )

    assert payload["status"] == "blocked"
    assert payload["proposals"][0]["reason"] == "no_available_slot"
