#!/usr/bin/env python3
"""Collect safe clue signals for a meeting prep candidate."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from meeting_context_note_ledger import DEFAULT_LEDGER_DB, MeetingContextNoteLedger

SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def collect_meeting_context_clues(
    meeting: dict[str, Any],
    *,
    note_ledger_db_path: str | Path | None = DEFAULT_LEDGER_DB,
    max_notes: int = 5,
    notes_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    terms = _terms(meeting)
    notes = notes_payload
    if notes is None:
        try:
            notes = MeetingContextNoteLedger(note_ledger_db_path).find_relevant(meeting, limit=max_notes)
        except Exception:
            notes = []
    safe_notes = [
        {
            "source_ref": note.get("source_ref"),
            "note_fingerprint": note.get("note_fingerprint"),
            "preview": _safe_text(note.get("note_preview") or note.get("note_text"), 220),
            "target_date": note.get("target_date"),
            "status": note.get("status"),
        }
        for note in (notes or [])[:max_notes]
    ]
    clue_refs = [f"calendar:{meeting.get('calendar_event_ref')}"]
    clue_refs.extend(str(note.get("source_ref")) for note in safe_notes if note.get("source_ref"))
    return {
        "status": "ok",
        "meeting_key": meeting.get("meeting_key"),
        "title": _safe_text(meeting.get("title"), 180),
        "query": " ".join(terms[:12]),
        "query_terms": terms[:20],
        "participant_domains": meeting.get("participant_domains") or [],
        "participant_count": int(meeting.get("participant_count") or 0),
        "calendar_description_hash": meeting.get("description_hash"),
        "calendar_description_clues": meeting.get("description_clues") or [],
        "discord_context_notes": safe_notes,
        "context_note_count": len(safe_notes),
        "source_refs": clue_refs[:20],
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _terms(meeting: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    terms.extend(str(item or "") for item in meeting.get("query_terms") or [])
    terms.extend(str(item or "") for item in meeting.get("participant_domains") or [])
    terms.extend(str(item or "") for item in meeting.get("description_clues") or [])
    terms.extend(re.findall(r"[A-Za-zÁ-ž0-9][A-Za-zÁ-ž0-9._-]{2,}", str(meeting.get("title") or "")))
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        clean = _safe_text(term, 80).lower()
        if len(clean) < 3 or clean in seen or clean in {"meeting", "call", "zoom"}:
            continue
        unique.append(clean)
        seen.add(clean)
    return unique


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def hash_payload(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
