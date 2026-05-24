#!/usr/bin/env python3
"""Durable ledger for Dusan's Discord context notes to Rocky."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER_DB = Path("/Users/clawdbot/.openclaw/workspace/improvement/meeting_context_notes.sqlite3")
VALID_STATUSES = {"seen", "associated", "used_in_brief", "ack_sent", "ack_failed", "ignored"}
SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def note_fingerprint(note: dict[str, Any]) -> str:
    payload = {
        "source_ref": note.get("source_ref") or "",
        "text_hash": hash_text(note.get("text") or note.get("note") or ""),
    }
    return f"meeting-note:{hash_text(json.dumps(payload, sort_keys=True, default=str))}"


class MeetingContextNoteLedger:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_LEDGER_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meeting_context_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    note_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note_preview TEXT,
                    note_text TEXT,
                    note_hash TEXT,
                    target_date TEXT,
                    associated_meeting_key TEXT,
                    ack_status TEXT,
                    ack_message_id TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    used_at TEXT,
                    acknowledged_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(source_ref, note_fingerprint)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_context_notes_recent ON meeting_context_notes(last_seen_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_context_notes_target ON meeting_context_notes(target_date, status)")
            conn.commit()

    def record_seen(self, note: dict[str, Any], *, status: str = "seen", reason: str | None = None) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid_meeting_context_note_status:{status}")
        now = utc_now_iso()
        source = str(note.get("source") or "Discord")
        source_ref = str(note.get("source_ref") or f"context-note:{hash_text(json.dumps(note, sort_keys=True, default=str))}")
        fingerprint = str(note.get("note_fingerprint") or note_fingerprint(note))
        text = str(note.get("text") or note.get("note") or "")
        safe_text = safe_text_value(text, 1200)
        metadata = {
            key: note.get(key)
            for key in ("channel_id", "message_id", "author_id_hash", "classification")
            if note.get(key) is not None
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meeting_context_notes (
                    source, source_ref, note_fingerprint, status, note_preview,
                    note_text, note_hash, target_date, reason, created_at,
                    first_seen_at, last_seen_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref, note_fingerprint) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN meeting_context_notes.status IN ('seen', 'ignored') THEN excluded.status ELSE meeting_context_notes.status END,
                    reason=COALESCE(excluded.reason, meeting_context_notes.reason),
                    metadata_json=excluded.metadata_json
                """,
                (
                    source,
                    source_ref,
                    fingerprint,
                    status,
                    safe_preview(text),
                    safe_text,
                    hash_text(text),
                    note.get("target_date"),
                    reason,
                    str(note.get("created_at") or now),
                    now,
                    now,
                    json.dumps(metadata, sort_keys=True, default=str),
                ),
            )
            conn.commit()
        return self.get(source_ref=source_ref, note_fingerprint=fingerprint) or {}

    def associate(self, *, source_ref: str, note_fingerprint: str, meeting_key: str, status: str = "associated") -> dict[str, Any]:
        if status not in {"associated", "used_in_brief"}:
            raise ValueError(f"invalid_association_status:{status}")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE meeting_context_notes
                SET status=?, associated_meeting_key=?, used_at=CASE WHEN ?='used_in_brief' THEN ? ELSE used_at END
                WHERE source_ref=? AND note_fingerprint=?
                """,
                (status, meeting_key, status, now, source_ref, note_fingerprint),
            )
            conn.commit()
        return self.get(source_ref=source_ref, note_fingerprint=note_fingerprint) or {}

    def update_ack(self, *, source_ref: str, note_fingerprint: str, status: str, message_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
        if status not in {"ack_sent", "ack_failed"}:
            raise ValueError(f"invalid_ack_status:{status}")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE meeting_context_notes
                SET status=?, ack_status=?, ack_message_id=?, reason=COALESCE(?, reason), acknowledged_at=?
                WHERE source_ref=? AND note_fingerprint=?
                """,
                (status, status, message_id, reason, now, source_ref, note_fingerprint),
            )
            conn.commit()
        return self.get(source_ref=source_ref, note_fingerprint=note_fingerprint) or {}

    def get(self, *, source_ref: str, note_fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM meeting_context_notes WHERE source_ref=? AND note_fingerprint=?",
                (source_ref, note_fingerprint),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meeting_context_notes ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def find_relevant(self, meeting: dict[str, Any], *, limit: int = 5, max_age_days: int = 14) -> list[dict[str, Any]]:
        target_date = str(meeting.get("date") or meeting.get("start_local") or "")[:10]
        terms = _meeting_terms(meeting)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM meeting_context_notes
                WHERE last_seen_at >= ?
                  AND status IN ('seen', 'associated', 'used_in_brief', 'ack_sent')
                ORDER BY last_seen_at DESC, id DESC
                LIMIT 100
                """,
                (cutoff,),
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in [_row_to_dict(item) for item in rows]:
            text = f"{row.get('note_text') or ''} {row.get('note_preview') or ''}".lower()
            date_match = bool(target_date and (row.get("target_date") == target_date or target_date in text))
            token_match = any(term and term in text for term in terms)
            if date_match or token_match:
                matches.append(row)
            if len(matches) >= max(1, int(limit)):
                break
        return matches


def safe_preview(value: Any, *, limit: int = 180) -> str:
    return safe_text_value(value, limit)


def safe_text_value(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _meeting_terms(meeting: dict[str, Any]) -> list[str]:
    terms = []
    terms.extend(str(item or "") for item in meeting.get("query_terms") or [])
    terms.extend(str(item or "") for item in meeting.get("participant_domains") or [])
    terms.extend(re.findall(r"[a-z0-9][a-z0-9._-]{3,}", str(meeting.get("title") or "").lower()))
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        safe = safe_text_value(term, 80).lower()
        if len(safe) >= 4 and safe not in seen:
            unique.append(safe)
            seen.add(safe)
    return unique[:16]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = {key: row[key] for key in row.keys()}
    metadata = payload.pop("metadata_json", None)
    try:
        payload["metadata"] = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        payload["metadata"] = {}
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Rocky meeting context notes.")
    parser.add_argument("--ledger-db")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    ledger = MeetingContextNoteLedger(args.ledger_db)
    payload = {"status": "ok", "notes": ledger.recent(limit=args.limit), "calendar_write_attempted": False, "notion_write_attempted": False}
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting context notes: {len(payload['notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
