#!/usr/bin/env python3
"""Promote meeting outcomes into guarded Obsidian investor relationship memory."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from obsidian_memory import promote_cross_agent_memory


SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def promote_meeting_outcome_memory(
    outcome: dict[str, Any],
    *,
    live: bool = False,
    promote_func: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidates = _memory_candidates(outcome)
    if not live:
        return {
            "status": "dry_run",
            "reason": "live_flag_not_supplied",
            "candidate_count": len(candidates),
            "memory_refs": [],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
            "obsidian_write_attempted": False,
        }
    refs: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    promoter = promote_func or promote_cross_agent_memory
    for candidate in candidates:
        result = promoter(**candidate)
        results.append({"status": result.get("status"), "reason": result.get("reason"), "path": result.get("path"), "note_type": result.get("note_type"), "action": result.get("action")})
        if result.get("path"):
            refs.append({"path": result.get("path"), "note_type": result.get("note_type"), "action": result.get("action")})
    status = "ok" if any(item.get("status") == "ok" for item in results) or not candidates else "blocked"
    return {
        "status": status,
        "reason": "meeting_outcome_memory_promoted" if status == "ok" else "obsidian_memory_promotion_blocked",
        "candidate_count": len(candidates),
        "results": results,
        "memory_refs": refs,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
        "obsidian_write_attempted": bool(candidates),
    }


def _memory_candidates(outcome: dict[str, Any]) -> list[dict[str, Any]]:
    title = _safe_text(outcome.get("title") or "Meeting outcome", 100)
    source_ref = _safe_text(outcome.get("source_ref"), 180)
    source_date = _safe_text(outcome.get("meeting_date"), 40)
    decisions = [_safe_text(item, 300) for item in outcome.get("decisions") or []]
    relationship = [_safe_text(item, 300) for item in outcome.get("relationship_updates") or []]
    open_questions = [_safe_text(item, 300) for item in outcome.get("open_questions") or []]
    body_lines = []
    if decisions:
        body_lines.append("## Decisions")
        body_lines.extend(f"- {item}" for item in decisions[:8])
    if relationship:
        body_lines.append("## Relationship updates")
        body_lines.extend(f"- {item}" for item in relationship[:8])
    if open_questions:
        body_lines.append("## Open questions")
        body_lines.extend(f"- {item}" for item in open_questions[:8])
    if not body_lines:
        return []
    return [
        {
            "agent_name": "Rocky",
            "note_type": "meeting",
            "title": title,
            "summary": _safe_text("; ".join((decisions + relationship + open_questions)[:3]) or title, 500),
            "body": "\n".join(body_lines),
            "tags": ["meeting-outcome", "investor-memory"],
            "source_ref": source_ref,
            "source_date": source_date,
            "dedupe_key": f"meeting-outcome-{_hash_text(outcome.get('meeting_key') or source_ref)}",
            "related_entities": [_safe_text(outcome.get("title"), 80)],
        }
    ]


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
