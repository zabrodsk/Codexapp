#!/usr/bin/env python3
"""Read-only Apple Mail metadata context for meeting prep.

This reader never changes Mail state. It reads Envelope Index metadata only and
returns bounded subject/summary previews plus stable source references.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_MAIL_DB_PATH = Path.home() / "Library/Mail/V10/MailData/Envelope Index"
SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def collect_meeting_email_context(
    meeting: dict[str, Any],
    *,
    mail_db_path: str | Path | None = None,
    since_days: int = 45,
    limit: int = 5,
) -> dict[str, Any]:
    path = Path(mail_db_path or DEFAULT_MAIL_DB_PATH).expanduser()
    if not path.exists():
        return {
            "status": "blocked",
            "reason": "mail_envelope_index_missing",
            "items": [],
            "item_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    terms = _search_terms(meeting)
    if not terms:
        return {
            "status": "skipped",
            "reason": "no_email_search_terms",
            "items": [],
            "item_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    try:
        rows = _query_mail(path, terms=terms, since_days=since_days, limit=limit)
    except sqlite3.Error as exc:
        return {
            "status": "blocked",
            "reason": "mail_metadata_query_failed",
            "error_hash": _hash_text(str(exc)),
            "items": [],
            "item_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    items = [_row_to_item(row) for row in rows]
    return {
        "status": "ok",
        "reason": "meeting_email_context_read",
        "search_term_count": len(terms),
        "items": items,
        "item_count": len(items),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _query_mail(path: Path, *, terms: list[str], since_days: int, limit: int) -> list[sqlite3.Row]:
    like_terms = [f"%{term.lower()}%" for term in terms[:8]]
    cutoff = int(time.time() - max(1, int(since_days)) * 86400)
    clauses = []
    params: list[Any] = []
    for term in like_terms:
        clauses.append("(LOWER(COALESCE(s.subject, '')) LIKE ? OR LOWER(COALESCE(a.address, '')) LIKE ? OR LOWER(COALESCE(a.comment, '')) LIKE ? OR LOWER(COALESCE(sm.summary, '')) LIKE ?)")
        params.extend([term, term, term, term])
    where = " OR ".join(clauses) or "1=0"
    query = f"""
    SELECT
      m.ROWID AS rowid,
      COALESCE(a.address, '') AS sender,
      COALESCE(a.comment, '') AS sender_name,
      COALESCE(s.subject, '') AS subject,
      COALESCE(sm.summary, '') AS summary,
      COALESCE(m.date_received, 0) AS date_received,
      COALESCE(m.read, 0) AS read_flag
    FROM messages m
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN summaries sm ON m.summary = sm.ROWID
    WHERE m.deleted = 0
      AND (m.date_received = 0 OR m.date_received >= ? OR m.date_received >= ?)
      AND ({where})
    ORDER BY m.date_received DESC
    LIMIT ?
    """
    params = [cutoff, cutoff - 978307200] + params + [max(1, min(int(limit), 25))]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    sender = str(row["sender"] or "")
    return {
        "source_ref": f"apple-mail:message:{_hash_text(str(row['rowid']))}",
        "sender_domain": sender.rsplit("@", 1)[-1].lower()[:120] if "@" in sender else None,
        "sender_hash": _hash_text(sender.lower()) if sender else None,
        "subject_preview": _safe_text(row["subject"], 140),
        "summary_preview": _safe_text(row["summary"], 240),
        "date_received": int(row["date_received"] or 0),
        "read_state": "read" if int(row["read_flag"] or 0) else "unread",
    }


def _search_terms(meeting: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    terms.extend(str(item or "") for item in meeting.get("query_terms") or [])
    terms.extend(str(item or "") for item in meeting.get("participant_domains") or [])
    for participant in meeting.get("participants") or []:
        if isinstance(participant, dict):
            terms.append(str(participant.get("domain") or ""))
            terms.append(str(participant.get("name") or ""))
    title = str(meeting.get("title") or "")
    terms.extend(re.findall(r"[A-Za-zÁ-ž0-9][A-Za-zÁ-ž0-9._-]{3,}", title))
    seen: set[str] = set()
    safe: list[str] = []
    for term in terms:
        clean = _safe_text(term, 80).lower()
        if len(clean) < 4 or clean in seen or clean in {"meeting", "call", "zoom"}:
            continue
        safe.append(clean)
        seen.add(clean)
    return safe[:12]


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read sanitized meeting email context.")
    parser.add_argument("--meeting-json", required=True)
    parser.add_argument("--mail-db-path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    meeting = json.loads(args.meeting_json)
    payload = collect_meeting_email_context(meeting, mail_db_path=args.mail_db_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Email context: {payload.get('item_count', 0)}")
    return 0 if payload.get("status") in {"ok", "skipped", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
