import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_scheduler_state import AssistantSchedulerState


def test_scheduler_state_initializes_schema(tmp_path):
    state = AssistantSchedulerState(tmp_path / "scheduler.sqlite3")

    assert state.path.exists()
    assert state.list_job_runs() == []
    assert state.list_dead_letters() == []


def test_records_job_runs(tmp_path):
    state = AssistantSchedulerState(tmp_path / "scheduler.sqlite3")

    row = state.record_job_run(
        job_name="betty_mail_triage",
        status="succeeded",
        idempotency_key="scheduler:test",
        job_label="Betty",
        summary="ok",
    )

    assert row["job_name"] == "betty_mail_triage"
    assert row["status"] == "succeeded"
    assert state.list_job_runs(job_name="betty_mail_triage")[0]["run_id"] == row["run_id"]


def test_dead_letter_upsert_is_idempotent_for_open_failure(tmp_path):
    state = AssistantSchedulerState(tmp_path / "scheduler.sqlite3")

    first = state.upsert_dead_letter(
        job_name="betty_mail_triage",
        workflow="betty_mail_triage",
        idempotency_key="scheduler:test",
        failure_class="launchagent_not_loaded",
        safe_summary="not loaded",
    )
    second = state.upsert_dead_letter(
        job_name="betty_mail_triage",
        workflow="betty_mail_triage",
        idempotency_key="scheduler:test",
        failure_class="launchagent_not_loaded",
        safe_summary="still not loaded",
    )

    assert first["dead_letter_id"] == second["dead_letter_id"]
    assert second["attempts"] == 2
    assert len(state.list_dead_letters()) == 1


def test_lock_record_blocks_duplicate_and_releases(tmp_path):
    state = AssistantSchedulerState(tmp_path / "scheduler.sqlite3")

    first, first_row = state.acquire_lock_record(
        lock_key="lock:test",
        workflow="test",
        idempotency_key="idem",
        owner="tester",
        pid=1,
        acquired_at="2026-05-22T10:00:00+00:00",
        expires_at="2026-05-22T10:10:00+00:00",
    )
    second, second_row = state.acquire_lock_record(
        lock_key="lock:test",
        workflow="test",
        idempotency_key="idem",
        owner="tester",
        pid=1,
        acquired_at="2026-05-22T10:01:00+00:00",
        expires_at="2026-05-22T10:11:00+00:00",
    )
    released = state.release_lock_record(lock_key="lock:test", released_at="2026-05-22T10:02:00+00:00")

    assert first == "acquired"
    assert first_row["status"] == "active"
    assert second == "duplicate_blocked"
    assert second_row["status"] == "active"
    assert released["status"] == "released"


def test_expired_lock_can_be_recovered(tmp_path):
    state = AssistantSchedulerState(tmp_path / "scheduler.sqlite3")
    state.acquire_lock_record(
        lock_key="lock:test",
        workflow="test",
        idempotency_key="idem",
        owner="tester",
        pid=1,
        acquired_at="2026-05-22T10:00:00+00:00",
        expires_at="2026-05-22T10:01:00+00:00",
    )

    result, row = state.acquire_lock_record(
        lock_key="lock:test",
        workflow="test",
        idempotency_key="idem",
        owner="tester2",
        pid=2,
        acquired_at="2026-05-22T10:02:00+00:00",
        expires_at="2026-05-22T10:12:00+00:00",
    )

    assert result == "stale_recovered"
    assert row["owner"] == "tester2"
