#!/usr/bin/env python3
"""Build sanitized pre-meeting briefs from enriched meeting context."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def build_meeting_prep_briefing(
    meeting: dict[str, Any],
    enriched_context: dict[str, Any],
    *,
    use_llm: bool = False,
    llm_func: Any | None = None,
) -> dict[str, Any]:
    if enriched_context.get("status") == "skipped_no_context":
        return {
            "status": "skipped_no_context",
            "reason": "no_relevant_context_found",
            "meeting_key": meeting.get("meeting_key"),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    deterministic = _deterministic_brief(meeting, enriched_context)
    llm_status = {"status": "skipped", "reason": "llm_disabled"}
    if use_llm and llm_func:
        llm_status = _llm_refine(deterministic, llm_func=llm_func)
        if llm_status.get("status") == "ok":
            deterministic.update(llm_status.get("brief") or {})
    message = render_meeting_prep_message(meeting, deterministic, enriched_context)
    return _redact(
        {
            "status": "ok",
            "reason": "meeting_prep_brief_built",
            "meeting_key": meeting.get("meeting_key"),
            "title": meeting.get("title"),
            "start_local": meeting.get("start_local"),
            "brief": deterministic,
            "llm": llm_status,
            "discord_message": message,
            "message_sha256": _hash_text(message),
            "message_chars": len(message),
            "source_refs": enriched_context.get("source_refs") or [],
            "confidence": enriched_context.get("confidence"),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    )


def render_meeting_prep_message(meeting: dict[str, Any], brief: dict[str, Any], enriched_context: dict[str, Any]) -> str:
    start = _safe_text(meeting.get("start_local"), 32)
    title = _safe_text(meeting.get("title"), 120)
    lines = [
        f"Rocky meeting prep - {start}",
        title,
        "",
        "Focus",
        f"- {brief.get('focus') or 'Use the context below to enter the meeting prepared.'}",
        "",
        "Relevant context",
    ]
    for item in (brief.get("context_points") or [])[:4]:
        lines.append(f"- {item}")
    if brief.get("open_loops"):
        lines.extend(["", "Open loops"])
        for item in (brief.get("open_loops") or [])[:4]:
            lines.append(f"- {item}")
    if brief.get("questions"):
        lines.extend(["", "Questions to ask"])
        for item in (brief.get("questions") or [])[:4]:
            lines.append(f"- {item}")
    if brief.get("dusan_notes"):
        lines.extend(["", "Dusan note"])
        for item in (brief.get("dusan_notes") or [])[:3]:
            lines.append(f"- {item}")
    refs = [str(ref) for ref in (enriched_context.get("source_refs") or [])[:6]]
    if refs:
        lines.extend(["", "Refs"])
        lines.append(", ".join(refs))
    return _safe_multiline("\n".join(lines), 1800)


def _deterministic_brief(meeting: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    memory_items = ((enriched.get("memory") or {}).get("items") or [])
    task_items = ((enriched.get("notion_tasks") or {}).get("items") or [])
    email_items = ((enriched.get("email_context") or {}).get("items") or [])
    notes = enriched.get("discord_context_notes") or []
    context_points: list[str] = []
    for item in memory_items[:3]:
        title = item.get("title") or "Obsidian context"
        summary = item.get("summary") or item.get("source_ref") or ""
        context_points.append(_safe_text(f"{title}: {summary}", 220))
    for item in email_items[:2]:
        context_points.append(_safe_text(f"Recent email signal from {item.get('sender_domain') or 'mail'}: {item.get('subject_preview')}", 180))
    open_loops = [
        _safe_text(f"{item.get('priority') or 'Normal'} task: {item.get('title')}", 180)
        for item in task_items[:4]
    ]
    dusan_notes = [_safe_text(note.get("preview") or note.get("note_preview"), 220) for note in notes[:3]]
    focus = _focus_line(meeting, memory_items, task_items, notes)
    questions = _questions(meeting, task_items, memory_items)
    if not context_points and dusan_notes:
        context_points.append("Dusan sent fresh context for this meeting.")
    if not context_points and open_loops:
        context_points.append("There are related open tasks to keep in view.")
    return {
        "focus": focus,
        "context_points": [item for item in context_points if item],
        "open_loops": [item for item in open_loops if item],
        "questions": questions,
        "dusan_notes": [item for item in dusan_notes if item],
        "done_signal": "Meeting completed with decisions and next actions captured.",
    }


def _focus_line(meeting: dict[str, Any], memory_items: list[dict[str, Any]], task_items: list[dict[str, Any]], notes: list[dict[str, Any]]) -> str:
    if notes:
        return "Use Dusan's fresh Discord note as the top intent signal, then ground it in memory and open tasks."
    if task_items:
        return "Resolve or advance the related open loops without creating new unclear commitments."
    if memory_items:
        return "Bring forward the durable relationship/project context and confirm what changed since the last touch."
    return f"Prepare around {_safe_text(meeting.get('title'), 100)}."


def _questions(meeting: dict[str, Any], task_items: list[dict[str, Any]], memory_items: list[dict[str, Any]]) -> list[str]:
    questions = []
    if task_items:
        questions.append("Which open item should be closed or explicitly moved forward after this meeting?")
    if memory_items:
        questions.append("What has changed since the last recorded context?")
    if meeting.get("participant_count"):
        questions.append("Who owns the next concrete action after the meeting?")
    return questions[:4]


def _llm_refine(brief: dict[str, Any], *, llm_func: Any) -> dict[str, Any]:
    try:
        raw = llm_func(json.dumps(brief, ensure_ascii=False, sort_keys=True))
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return {"status": "ok", "brief": parsed if isinstance(parsed, dict) else {}}
    except Exception as exc:
        return {"status": "degraded", "reason": "meeting_prep_llm_failed", "error_hash": _hash_text(str(exc))}


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_multiline(value: Any, limit: int = 1800) -> str:
    lines = [re.sub(r"[ \t]+", " ", SENSITIVE_RE.sub("[redacted]", line)).strip() for line in str(value or "").splitlines()]
    return "\n".join(lines).strip()[:limit]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _safe_multiline(value, 2000) if "\n" in value else _safe_text(value, 1000)
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a sanitized meeting prep brief from JSON inputs.")
    parser.add_argument("--meeting-json", required=True)
    parser.add_argument("--context-json", required=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = build_meeting_prep_briefing(json.loads(args.meeting_json), json.loads(args.context_json))
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload.get("discord_message") or payload.get("reason"))
    return 0 if payload.get("status") in {"ok", "skipped_no_context"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
