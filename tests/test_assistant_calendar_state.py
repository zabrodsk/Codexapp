import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_calendar_state import AssistantCalendarState, SCHEMA_VERSION


def test_calendar_state_records_and_reads_idempotency_alias(tmp_path):
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    state.record_created(
        idempotency_key="rocky:training:2026-05-27:old",
        calendar_name="Calendar",
        title="Rocky: Training - Run: Recovery Run 60 min",
        start="2026-05-27T08:00:00+02:00",
        end="2026-05-27T10:30:00+02:00",
        event_uid="uid-old",
        create_audit_id="audit-old",
        metadata={"source_refs": ["trainingpeaks:old"]},
    )

    alias = state.record_alias(
        alias_idempotency_key="rocky:training:2026-05-27:new",
        canonical_idempotency_key="rocky:training:2026-05-27:old",
        reason="source_ref_drift_verified",
        metadata={"source_ref": "trainingpeaks:new"},
    )

    assert alias["alias_idempotency_key"] == "rocky:training:2026-05-27:new"
    assert alias["canonical_idempotency_key"] == "rocky:training:2026-05-27:old"
    assert state.resolve_alias("rocky:training:2026-05-27:new")["idempotency_key"] == "rocky:training:2026-05-27:old"
    assert state.get_schema_version() == SCHEMA_VERSION


def test_calendar_state_lists_aliases_for_canonical_key(tmp_path):
    state = AssistantCalendarState(tmp_path / "assistant_calendar.sqlite3")
    state.record_alias(
        alias_idempotency_key="alias-one",
        canonical_idempotency_key="canonical",
        reason="source_ref_drift_verified",
    )
    state.record_alias(
        alias_idempotency_key="alias-two",
        canonical_idempotency_key="canonical",
        reason="source_ref_drift_verified",
    )

    aliases = state.list_aliases(canonical_idempotency_key="canonical")

    assert {row["alias_idempotency_key"] for row in aliases} == {"alias-one", "alias-two"}
