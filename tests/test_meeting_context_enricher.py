import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_context_enricher import enrich_meeting_context


def _meeting():
    return {
        "meeting_key": "meeting:1",
        "title": "Jana portfolio update",
        "date": "2026-05-25",
        "start_local": "2026-05-25 10:00:00",
        "query_terms": ["jana", "portfolio"],
        "participant_domains": ["example.com"],
        "participants": [{"name": "Jana", "domain": "example.com"}],
        "calendar_event_ref": "apple-calendar:event:1",
        "description_hash": "abc",
        "description_clues": ["portfolio"],
    }


def test_enrichment_uses_primary_memory_tasks_email_and_notes():
    payload = enrich_meeting_context(
        _meeting(),
        calendar_clues_payload={
            "status": "ok",
            "query": "jana portfolio example.com",
            "discord_context_notes": [{"source_ref": "discord-context:1", "preview": "Ask about runway."}],
            "source_refs": ["calendar:1", "discord-context:1"],
        },
        obsidian_query_func=lambda *args, **kwargs: {
            "status": "ok",
            "results": [{"title": "Jana", "path": "people/jana.md", "snippet": "Portfolio context and prior decision."}],
        },
        notion_tasks_payload={
            "status": "ok",
            "tasks": [{"title": "Follow up with Jana", "status": "Open", "priority": "High", "source_ref": "task:1"}],
        },
        email_context_payload={
            "status": "ok",
            "items": [{"source_ref": "apple-mail:1", "sender_domain": "example.com", "subject_preview": "Portfolio update"}],
        },
    )

    assert payload["status"] == "ok"
    assert payload["context_count"] == 4
    assert payload["memory"]["items"][0]["source"] == "Obsidian"
    assert payload["notion_tasks"]["items"][0]["title"] == "Follow up with Jana"
    assert "discord-context:1" in payload["source_refs"]


def test_enrichment_skips_when_no_context_found():
    payload = enrich_meeting_context(
        _meeting(),
        calendar_clues_payload={"status": "ok", "query": "jana", "discord_context_notes": [], "source_refs": []},
        obsidian_query_func=lambda *args, **kwargs: {"status": "ok", "results": []},
        notion_tasks_payload={"status": "ok", "tasks": []},
        email_context_payload={"status": "ok", "items": []},
    )

    assert payload["status"] == "skipped_no_context"
    assert payload["calendar_write_attempted"] is False


def test_enrichment_filters_weak_cross_topic_matches_for_mixed_meeting():
    meeting = {
        "meeting_key": "meeting:flo",
        "title": "FLO & RockawayQ",
        "date": "2026-05-26",
        "start_local": "2026-05-26 13:30:00",
        "query_terms": ["flo", "rockawayq", "teams.microsoft.com", "377610771594863"],
        "participant_domains": ["weareflo.com", "rockaway.cz", "gmail.com"],
        "participants": [{"name": "Andrej Hájek Flo", "domain": "weareflo.com"}],
        "calendar_event_ref": "apple-calendar:event:flo",
        "description_hash": "abc",
        "description_clues": ["flo", "rockawayq", "microsoft", "teams", "url"],
    }

    payload = enrich_meeting_context(
        meeting,
        calendar_clues_payload={
            "status": "ok",
            "query": "flo rockawayq teams",
            "query_terms": ["flo", "rockawayq", "teams.microsoft.com", "377610771594863"],
            "discord_context_notes": [],
            "source_refs": ["calendar:1"],
        },
        obsidian_query_func=lambda *args, **kwargs: {
            "status": "ok",
            "results": [
                {"title": "Flo / FLO Group", "path": "companies/flo.md", "snippet": "@@ -1,4 @@ **Active acquisition pipeline:** NextLevel"},
                {"title": "RockawayQ press", "path": "projects/rockawayq-mtx.md", "snippet": "Review MTX press-release wording."},
            ],
        },
        notion_tasks_payload={
            "status": "ok",
            "tasks": [
                {"title": "Review and approve final RockawayQ / MTX press-release wording", "priority": "Normal", "source_ref": "task:mtx"},
                {"title": "Prepare FLO commercial setup materials", "priority": "High", "source_ref": "task:flo"},
            ],
        },
        email_context_payload={
            "status": "ok",
            "items": [
                {"source_ref": "apple-mail:flo", "sender_domain": "rockaway.cz", "subject_preview": "[focus] FLO Spin-off"},
                {"source_ref": "apple-mail:manifesto", "sender_domain": "rockaway.cz", "subject_preview": "Manifesto - Accounts Review"},
            ],
        },
    )

    assert payload["status"] == "ok"
    assert payload["relevance"]["clarification_needed"] is False
    assert "teams.microsoft.com" not in payload["relevance"]["topic_terms"]
    assert [item["title"] for item in payload["notion_tasks"]["items"]] == ["Prepare FLO commercial setup materials"]
    assert [item["subject_preview"] for item in payload["email_context"]["items"]] == ["[focus] FLO Spin-off"]
    assert all("MTX" not in item["title"] for item in payload["memory"]["items"])
