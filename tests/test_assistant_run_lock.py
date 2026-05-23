import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_run_lock import acquire_run_lock, release_run_lock, smoke_lock_cycle


def test_acquire_blocks_duplicate_and_release_allows_reacquire(tmp_path):
    db_path = tmp_path / "scheduler.sqlite3"
    ledger_path = tmp_path / "assistant_audit.jsonl"

    first = acquire_run_lock(
        workflow="test",
        idempotency_key="idem",
        db_path=db_path,
        ledger_path=ledger_path,
    )
    second = acquire_run_lock(
        workflow="test",
        idempotency_key="idem",
        db_path=db_path,
        ledger_path=ledger_path,
    )
    release_run_lock(
        workflow="test",
        idempotency_key="idem",
        db_path=db_path,
        ledger_path=ledger_path,
    )
    third = acquire_run_lock(
        workflow="test",
        idempotency_key="idem",
        db_path=db_path,
        ledger_path=ledger_path,
    )

    assert first.acquired is True
    assert second.status == "duplicate_blocked"
    assert third.acquired is True
    event_types = [json.loads(line)["event_type"] for line in ledger_path.read_text().splitlines()]
    assert "lock.acquired" in event_types
    assert "lock.duplicate_blocked" in event_types
    assert "lock.released" in event_types


def test_smoke_lock_cycle_does_not_run_helpers(tmp_path):
    payload = smoke_lock_cycle(
        workflow="smoke",
        idempotency_key="idem",
        db_path=tmp_path / "scheduler.sqlite3",
        ledger_path=tmp_path / "assistant_audit.jsonl",
    )

    assert payload["status"] == "ok"
    assert payload["first"]["status"] == "acquired"
    assert payload["second"]["status"] == "duplicate_blocked"
    assert payload["helpers_run"] is False
    assert payload["calendar_write_attempted"] is False
