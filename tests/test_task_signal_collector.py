import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_signal_collector import collect_email_task_signals, collect_obsidian_task_signals, collect_task_signals


def test_collect_email_task_signals_uses_betty_evaluations_without_raw_body_or_subject():
    helper_payload = {
        "status": "ok",
        "checked_at": "2026-05-23T10:00:00+00:00",
        "messages": [
            {
                "message_id": "m1",
                "subject": "Sensitive subject",
                "body_excerpt": "Sensitive body",
            }
        ],
        "evaluations": [
            {
                "message_id": "m1",
                "important": True,
                "priority": "soon",
                "short_summary": "Dusan should reply to the investor update.",
                "importance_reason": "Needs Dusan action.",
                "awareness_point": "Reply soon.",
            }
        ],
    }

    signals = collect_email_task_signals(helper_payload=helper_payload)

    assert len(signals) == 1
    encoded = json.dumps(signals)
    assert "Sensitive body" not in encoded
    assert "Sensitive subject" not in encoded
    assert signals[0]["source"] == "Email"
    assert signals[0]["requires_dusan_action_hint"] is True


def test_collect_obsidian_task_signals_turns_snippets_into_memory_signals():
    signals = collect_obsidian_task_signals(
        kind="memory",
        results=[
            {
                "title": "Open Loop",
                "path": "OpenClaw Memory/open-loops.md",
                "snippet": "Dusan needs to review the project decision.",
            }
        ],
    )

    assert signals[0]["source"] == "Memory"
    assert signals[0]["requires_dusan_action_hint"] is True
    assert signals[0]["source_ref"].startswith("obsidian:")


def test_collect_obsidian_task_signals_skips_memory_diff_noise():
    signals = collect_obsidian_task_signals(
        kind="memory",
        results=[
            {
                "title": "Dusan Profile — Maintenance Prompt",
                "path": "profile.md",
                "snippet": "@@ -5,4 @@ last_updated: 2026-04-24 hand-maintained",
            }
        ],
    )

    assert signals == []


def test_collect_task_signals_combines_sources_without_writes():
    payload = collect_task_signals(
        sources=["email", "memory"],
        helper_payload={"status": "ok", "messages": [], "evaluations": []},
        memory_results=[{"title": "Task", "path": "task.md", "snippet": "Dusan should send the note."}],
    )

    assert payload["status"] == "ok"
    assert payload["signal_count"] == 1
    assert payload["calendar_write_attempted"] is False
    assert payload["notion_write_attempted"] is False
