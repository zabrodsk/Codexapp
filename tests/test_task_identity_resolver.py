import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from notion_task_manager import legacy_source_dedupe_key, stable_task_action_fingerprint, stable_task_dedupe_key
from task_identity_resolver import resolve_task_identities


def _candidate(title, *, source_ref="apple-mail:message:abc", project="UF", confidence=0.9):
    task = {
        "title": title,
        "description": f"Dusan should handle: {title}",
        "source": "Email",
        "source_ref": source_ref,
        "owner": "Dusan",
        "status": "Open",
        "priority": "High",
        "confidence": confidence,
        "requires_dusan_action": True,
        "estimated_effort_minutes": 30,
        "related_project": project,
        "related_person_company": "United Founders",
        "auto_create_allowed": confidence >= 0.8,
    }
    task["action_fingerprint"] = stable_task_action_fingerprint(task)
    task["dedupe_key"] = stable_task_dedupe_key(task)
    return task


def test_one_email_can_create_two_distinct_task_identities():
    first = _candidate("Decide lead-agent integration path", project="Lead intake")
    second = _candidate("Align investment thesis with United Founders", project="Investment thesis")

    payload = resolve_task_identities([first, second], existing_tasks=[])

    assert payload["create_count"] == 2
    assert payload["resolved_count"] == 2
    keys = {item["task"]["dedupe_key"] for item in payload["results"]}
    assert len(keys) == 2
    assert all("action_fingerprint" in item["task"] for item in payload["results"])


def test_legacy_source_ref_task_migrates_only_matching_action():
    source_ref = "apple-mail:message:abc"
    lead = _candidate("Decide lead-agent integration path", source_ref=source_ref, project="Lead intake")
    thesis = _candidate("Align investment thesis with United Founders", source_ref=source_ref, project="Investment thesis")
    existing = {
        "page_id": "legacy-page",
        "title": "Confirm whether the lead agent can send leads directly to a UF webhook",
        "description": "Review webhook/API integration for lead agent intake.",
        "source": "Email",
        "source_ref": source_ref,
        "owner": "Dusan",
        "status": "Open",
        "priority": "High",
        "related_project": "Lead automation / UF webhook integration",
        "dedupe_key": legacy_source_dedupe_key({"source_ref": source_ref, "owner": "Dusan"}),
        "action_fingerprint": "",
        "detection_count": 1,
    }

    payload = resolve_task_identities([lead, thesis], existing_tasks=[existing])

    by_title = {item["task"]["title"]: item for item in payload["results"]}
    assert by_title[lead["title"]]["action"] == "update"
    assert by_title[lead["title"]]["existing_page_id"] == "legacy-page"
    assert by_title[lead["title"]]["migration"] == "legacy_source_ref_to_action_identity"
    assert by_title[thesis["title"]]["action"] == "create"
    assert payload["migrated_count"] == 1


def test_source_ref_fallback_does_not_blindly_merge_different_action():
    source_ref = "apple-mail:message:abc"
    incoming = _candidate("Align investment thesis with United Founders", source_ref=source_ref, project="Investment thesis")
    existing = {
        "page_id": "legacy-page",
        "title": "Confirm whether lead agent webhook can receive leads",
        "description": "Webhook integration task.",
        "source": "Email",
        "source_ref": source_ref,
        "owner": "Dusan",
        "status": "Open",
        "related_project": "Lead automation",
        "dedupe_key": legacy_source_dedupe_key({"source_ref": source_ref, "owner": "Dusan"}),
        "action_fingerprint": "",
    }

    payload = resolve_task_identities([incoming], existing_tasks=[existing])

    assert payload["results"][0]["action"] == "manual_review_required"
    assert payload["results"][0]["reason"] == "legacy_source_ref_action_mismatch"


def test_terminal_existing_task_is_not_reopened_or_chased():
    task = _candidate("Reply to investor", source_ref="apple-mail:message:done")
    existing = {
        "page_id": "done-page",
        "title": task["title"],
        "source": "Email",
        "source_ref": task["source_ref"],
        "status": "Done",
        "dedupe_key": task["dedupe_key"],
        "action_fingerprint": task["action_fingerprint"],
    }

    payload = resolve_task_identities([task], existing_tasks=[existing])

    assert payload["results"][0]["action"] == "terminal_match_skipped"
    assert payload["terminal_skipped_count"] == 1
