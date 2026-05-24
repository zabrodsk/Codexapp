import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_outcome_ledger import MeetingOutcomeLedger


def test_ledger_records_seen_then_applied(tmp_path):
    ledger = MeetingOutcomeLedger(tmp_path / "meeting_outcome.sqlite3")
    candidate = {"meeting_key": "meeting:1", "source_ref": "obsidian:1", "title": "Investor", "meeting_date": "2026-05-23"}

    seen = ledger.record_seen(candidate)
    applied = ledger.record_outcome({**candidate, "outcome_hash": "hash1", "follow_up_count": 1}, status="applied", task_refs=[{"page_id": "page1"}])

    assert seen["status"] == "seen"
    assert applied["status"] == "applied"
    assert ledger.counts_by_status()["seen"] == 1
    assert ledger.counts_by_status()["applied"] == 1
    assert ledger.recent(limit=1)[0]["task_refs"][0]["page_id"] == "page1"
