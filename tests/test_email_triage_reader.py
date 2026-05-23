import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from email_triage_reader import collect_email_attention


def _helper_payload():
    return {
        "status": "ok",
        "checked_at": "2026-05-25T09:30:00+02:00",
        "messages_found": 3,
        "messages": [
            {
                "message_id": "msg-1",
                "subject": "Secret deal",
                "sender_email": "founder@example.com",
                "preview": "token=abc raw body",
                "body_excerpt": "secret body",
            },
            {"message_id": "msg-2", "subject": "FYI"},
            {"message_id": "msg-3", "subject": "Later"},
        ],
        "evaluations": [
            {"message_id": "msg-1", "important": True, "priority": "urgent", "short_summary": "raw summary"},
            {"message_id": "msg-2", "important": False, "priority": "ignore"},
            {"message_id": "msg-3", "important": True, "priority": "soon"},
        ],
        "report": "full raw report",
    }


def test_collect_email_attention_sanitizes_helper_output():
    payload = collect_email_attention(helper_payload=_helper_payload())
    rendered = json.dumps(payload)

    assert payload["status"] == "ok"
    assert payload["unread_count"] == 3
    assert payload["attention_count"] == 2
    assert payload["priority_buckets"] == {"urgent": 1, "soon": 1}
    assert payload["source_refs"][0].startswith("apple-mail:message:")
    assert "Secret deal" not in rendered
    assert "founder@example.com" not in rendered
    assert "secret body" not in rendered
    assert "raw summary" not in rendered
    assert "full raw report" not in rendered


def test_collect_email_attention_blocks_on_helper_error_without_raw_error():
    payload = collect_email_attention(
        helper_payload={
            "status": "error",
            "error": "token super-secret-token failed for Subject: sensitive",
        }
    )
    rendered = json.dumps(payload)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "email_attention_evaluation_failed"
    assert "super-secret-token" not in rendered
    assert "sensitive" not in rendered
