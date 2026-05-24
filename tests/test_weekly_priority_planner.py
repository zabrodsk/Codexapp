import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from weekly_priority_planner import plan_weekly_priorities


def test_weekly_planner_detects_priorities_risks_and_learning_pending():
    signals = {
        "status": "ok",
        "week_label": "2026-W22",
        "calendar": {"days": [{"date": "2026-05-25", "max_focus_window_minutes": 120}]},
        "tasks": {"open_count": 2, "top_tasks": [{"title": "Urgent thing", "priority": "Urgent", "page_id": "p1", "source_ref": "s1"}]},
        "coding": {"top_items": [{"project": "Rocky", "recommended_next_step": "finish weekly review"}]},
        "scheduler": {"states": {"daily": {}}, "problem_jobs": ["assistant_learning"]},
        "dead_letters": {"open_count": 1},
        "calendar_hygiene": {"issue_count": 2, "duplicate_count": 1, "overbooked_days": [{"date": "2026-05-26"}]},
        "learning": {"status": "ok", "active_bounded_count": 0, "outcome_count": 10, "proposal_count": 3},
    }
    payload = plan_weekly_priorities(signals)

    assert payload["status"] == "ok"
    assert payload["do_first"][0]["title"] == "Urgent thing"
    assert any(item["category"] == "automation" for item in payload["risks_or_overloaded_days"])
    assert payload["calendar_hygiene"]["summary"] != "clean"
    assert payload["recommended_adjustments"][0]["category"] == "learning"
    assert payload["calendar_write_attempted"] is False
