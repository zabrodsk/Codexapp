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

NOISE_TERMS = {
    "call",
    "join",
    "meeting",
    "microsoft",
    "teams",
    "teams.microsoft.com",
    "zoom",
    "url",
}
GENERIC_DOMAINS = {"gmail.com", "rockaway.cz", "rockawaycapital.com"}
INTERNAL_PERSON_TERMS = {"dušan", "dusan", "zábrodský", "zabrodsky", "michal", "šmída", "smida"}


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
    terms.extend(str(item or "") for item in meeting.get("participant_domains") or [] if item not in GENERIC_DOMAINS)
    for participant in meeting.get("participants") or []:
        if isinstance(participant, dict) and not participant.get("is_self") and str(participant.get("domain") or "").lower() not in GENERIC_DOMAINS:
            terms.extend(re.findall(r"[A-Za-zÁ-ž0-9][A-Za-zÁ-ž0-9._-]{2,}", str(participant.get("name") or "")))
    terms.extend(str(item or "") for item in meeting.get("description_clues") or [])
    terms.extend(re.findall(r"[A-Za-zÁ-ž0-9][A-Za-zÁ-ž0-9._-]{2,}", str(meeting.get("title") or "")))
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        clean = _safe_text(term, 80).lower()
        if not _is_useful_term(clean) or clean in seen:
            continue
        unique.append(clean)
        seen.add(clean)
    return unique


def _is_useful_term(term: str) -> bool:
    if len(term) < 3 or term in NOISE_TERMS or term in INTERNAL_PERSON_TERMS:
        return False
    if len(term) >= 6 and re.search(r"\d", term):
        return False
    if re.fullmatch(r"[0-9a-f-]{8,}", term):
        return False
    if "safelinks" in term or "teams.microsoft" in term:
        return False
    return True


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def hash_payload(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]
