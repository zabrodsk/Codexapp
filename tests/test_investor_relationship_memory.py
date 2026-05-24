import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from investor_relationship_memory import promote_meeting_outcome_memory


def test_memory_promotion_is_dry_run_without_live():
    payload = promote_meeting_outcome_memory({"title": "Investor", "decisions": ["Send update"], "source_ref": "meeting:1"}, live=False)

    assert payload["status"] == "dry_run"
    assert payload["obsidian_write_attempted"] is False


def test_memory_promotion_uses_guarded_obsidian_promoter():
    calls = []

    def fake_promote(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "path": "/vault/meeting.md", "note_type": kwargs["note_type"], "action": "created"}

    payload = promote_meeting_outcome_memory(
        {"meeting_key": "meeting:1", "title": "Investor", "decisions": ["Send update"], "source_ref": "meeting:1"},
        live=True,
        promote_func=fake_promote,
    )

    assert payload["status"] == "ok"
    assert calls[0]["note_type"] == "meeting"
    assert payload["memory_refs"][0]["path"] == "/vault/meeting.md"
