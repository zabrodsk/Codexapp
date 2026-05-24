#!/usr/bin/env python3
"""Enrich meeting prep candidates with Obsidian, tasks, email, and notes."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

from meeting_context_collector import collect_meeting_context_clues
from meeting_email_context_reader import collect_meeting_email_context
from notion_task_manager import list_open_tasks
from obsidian_memory import query_obsidian_vault

SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def enrich_meeting_context(
    meeting: dict[str, Any],
    *,
    note_ledger_db_path: str | Path | None = None,
    calendar_clues_payload: dict[str, Any] | None = None,
    obsidian_query_func: Callable[..., dict[str, Any]] | None = None,
    notion_tasks_payload: dict[str, Any] | None = None,
    email_context_payload: dict[str, Any] | None = None,
    email_reader_func: Callable[..., dict[str, Any]] | None = None,
    include_email: bool = True,
    include_memory: bool = True,
) -> dict[str, Any]:
    clues = calendar_clues_payload or collect_meeting_context_clues(meeting, note_ledger_db_path=note_ledger_db_path)
    query = str(clues.get("query") or meeting.get("title") or "")
    memory = _query_memory(query, obsidian_query_func=obsidian_query_func) if include_memory and query else _disabled("memory_disabled")
    tasks = notion_tasks_payload if notion_tasks_payload is not None else _safe_task_context(meeting)
    email = email_context_payload
    if email is None:
        email = (email_reader_func or collect_meeting_email_context)(meeting) if include_email else _disabled("email_disabled")

    memory_items = _memory_items(memory)
    task_items = _task_items(tasks, meeting)
    email_items = _email_items(email)
    note_items = clues.get("discord_context_notes") or []
    source_refs = []
    for group in (memory_items, task_items, email_items, note_items):
        for item in group:
            ref = item.get("source_ref") or item.get("path") or item.get("page_id")
            if ref:
                source_refs.append(str(ref))
    context_count = len(memory_items) + len(task_items) + len(email_items) + len(note_items)
    status = "ok" if context_count else "skipped_no_context"
    reason = "meeting_context_enriched" if context_count else "no_relevant_context_found"
    return _redact(
        {
            "status": status,
            "reason": reason,
            "meeting_key": meeting.get("meeting_key"),
            "title": meeting.get("title"),
            "clues": clues,
            "memory": {
                "status": memory.get("status"),
                "reason": memory.get("reason"),
                "items": memory_items[:6],
                "item_count": len(memory_items),
            },
            "notion_tasks": {
                "status": tasks.get("status"),
                "reason": tasks.get("reason"),
                "items": task_items[:8],
                "item_count": len(task_items),
            },
            "email_context": {
                "status": email.get("status"),
                "reason": email.get("reason"),
                "items": email_items[:5],
                "item_count": len(email_items),
            },
            "discord_context_notes": note_items[:5],
            "context_count": context_count,
            "source_refs": source_refs[:30],
            "confidence": _confidence(context_count, memory_items, task_items, note_items),
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    )


def _query_memory(query: str, *, obsidian_query_func: Callable[..., dict[str, Any]] | None) -> dict[str, Any]:
    try:
        return (obsidian_query_func or query_obsidian_vault)(query, limit=6, mode="query")
    except Exception as exc:
        return {"status": "blocked", "reason": "obsidian_query_failed", "error_hash": _hash_text(str(exc)), "results": []}


def _safe_task_context(meeting: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = list_open_tasks(limit=80)
    except Exception as exc:
        return {"status": "blocked", "reason": "notion_task_query_failed", "error_hash": _hash_text(str(exc)), "tasks": []}
    if payload.get("status") != "ok":
        return payload
    terms = _terms(meeting)
    matched = []
    for task in payload.get("tasks") or []:
        haystack = " ".join(str(task.get(key) or "") for key in ("title", "description", "related_project", "related_person_company", "source_ref")).lower()
        if any(term in haystack for term in terms):
            matched.append(task)
    return {**payload, "tasks": matched[:12]}


def _memory_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        title = result.get("title") or result.get("path") or result.get("file") or result.get("source") or "Obsidian note"
        snippet = result.get("snippet") or result.get("text") or result.get("summary") or result.get("content") or ""
        items.append(
            {
                "source": "Obsidian",
                "title": _safe_text(title, 160),
                "source_ref": _safe_text(result.get("path") or result.get("source") or result.get("id") or title, 240),
                "summary": _safe_text(snippet, 280),
                "evidence_hash": _hash_text(json.dumps(result, sort_keys=True, default=str)),
            }
        )
    return items


def _task_items(payload: dict[str, Any], meeting: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for task in payload.get("tasks") or []:
        items.append(
            {
                "source": "Notion",
                "title": _safe_text(task.get("title"), 160),
                "status": task.get("status"),
                "priority": task.get("priority"),
                "page_id": task.get("page_id"),
                "source_ref": task.get("source_ref"),
                "related_project": _safe_text(task.get("related_project"), 160),
                "related_person_company": _safe_text(task.get("related_person_company"), 160),
            }
        )
    return items


def _email_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": "Apple Mail",
            "source_ref": item.get("source_ref"),
            "sender_domain": item.get("sender_domain"),
            "subject_preview": _safe_text(item.get("subject_preview"), 140),
            "summary_preview": _safe_text(item.get("summary_preview"), 180),
            "read_state": item.get("read_state"),
        }
        for item in (payload.get("items") or [])
    ]


def _terms(meeting: dict[str, Any]) -> list[str]:
    terms = []
    terms.extend(str(item or "").lower() for item in meeting.get("query_terms") or [])
    terms.extend(str(item or "").lower() for item in meeting.get("participant_domains") or [])
    terms.extend(re.findall(r"[a-z0-9][a-z0-9._-]{3,}", str(meeting.get("title") or "").lower()))
    return [term for term in terms if len(term) >= 4][:20]


def _confidence(context_count: int, memory_items: list[dict[str, Any]], task_items: list[dict[str, Any]], note_items: list[dict[str, Any]]) -> float:
    score = 0.2
    score += min(len(memory_items), 3) * 0.15
    score += min(len(task_items), 3) * 0.10
    score += min(len(note_items), 2) * 0.18
    if context_count:
        score += 0.12
    return round(min(score, 0.92), 2)


def _disabled(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "results": [], "items": [], "tasks": []}


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 1000)
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
