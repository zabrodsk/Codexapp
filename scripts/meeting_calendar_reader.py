#!/usr/bin/env python3
"""Read upcoming Apple Calendar meetings with participant metadata.

This is deliberately read-only. Meeting descriptions are used only to derive
safe clue tokens and hashes; raw descriptions never leave this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
APPLE_EPOCH_OFFSET = 978307200
SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
STOPWORDS = {
    "and", "are", "call", "calendar", "com", "for", "from", "http", "https",
    "meeting", "meet", "prep", "the", "this", "with", "zoom",
}


def read_upcoming_meetings(
    *,
    planning_date: str | date | None = None,
    days: int = 1,
    db_path: str | Path | None = None,
    include_all_day: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Return sanitized non-Rocky meeting candidates for the date range."""
    day = _parse_date(planning_date) if planning_date else date.today()
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=max(1, int(days)))
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    if not path.exists():
        return {
            "status": "blocked",
            "reason": "calendar_db_missing",
            "meetings": [],
            "meeting_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    try:
        events = _query_events(path, start=start, end=end, include_all_day=include_all_day)
        participants = _query_participants(path, [event["event_rowid"] for event in events])
    except sqlite3.Error as exc:
        return {
            "status": "blocked",
            "reason": "calendar_db_query_failed",
            "error_hash": _hash_text(str(exc)),
            "meetings": [],
            "meeting_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    meetings: list[dict[str, Any]] = []
    for event in events:
        if _skip_event(event):
            continue
        event_participants = participants.get(str(event["event_rowid"]), [])
        meeting = _event_to_meeting(event, event_participants)
        meetings.append(meeting)
        if len(meetings) >= max(1, int(limit)):
            break
    return {
        "status": "ok",
        "reason": "calendar_meetings_read",
        "planning_date": day.isoformat(),
        "days": max(1, int(days)),
        "meetings": meetings,
        "meeting_count": len(meetings),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _query_events(path: Path, *, start: datetime, end: datetime, include_all_day: bool) -> list[dict[str, Any]]:
    query = """
    SELECT
      ci.ROWID AS event_rowid,
      COALESCE(ci.summary, '') AS summary,
      datetime(ci.start_date + ?, 'unixepoch', 'localtime') AS start_local,
      datetime(ci.end_date + ?, 'unixepoch', 'localtime') AS end_local,
      COALESCE(ci.all_day, 0) AS all_day,
      COALESCE(c.title, '') AS calendar,
      TRIM(COALESCE(loc.title, '') || CASE
        WHEN loc.address IS NOT NULL AND loc.address != '' AND loc.title IS NOT NULL AND loc.title != '' THEN ' | '
        ELSE ''
      END || COALESCE(loc.address, '')) AS location,
      COALESCE(ci.description, '') AS description,
      COALESCE(ci.has_attendees, 0) AS has_attendees
    FROM CalendarItem ci
    LEFT JOIN Calendar c ON ci.calendar_id = c.ROWID
    LEFT JOIN Location loc ON loc.item_owner_id = ci.ROWID
    WHERE datetime(ci.start_date + ?, 'unixepoch', 'localtime') < ?
      AND datetime(ci.end_date + ?, 'unixepoch', 'localtime') > ?
    ORDER BY ci.start_date
    """
    params = (
        APPLE_EPOCH_OFFSET,
        APPLE_EPOCH_OFFSET,
        APPLE_EPOCH_OFFSET,
        end.strftime("%Y-%m-%d %H:%M:%S"),
        APPLE_EPOCH_OFFSET,
        start.strftime("%Y-%m-%d %H:%M:%S"),
    )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    events = [dict(row) for row in rows]
    if include_all_day:
        return events
    return [event for event in events if not bool(event.get("all_day"))]


def _query_participants(path: Path, event_ids: list[Any]) -> dict[str, list[dict[str, Any]]]:
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    query = f"""
    SELECT
      p.owner_id AS event_rowid,
      COALESCE(p.email, i.address, '') AS email,
      CASE
        WHEN COALESCE(i.display_name, '') != '' THEN COALESCE(i.display_name, '')
        ELSE TRIM(COALESCE(i.first_name, '') || ' ' || COALESCE(i.last_name, ''))
      END AS display_name,
      COALESCE(i.address, '') AS address,
      COALESCE(p.is_self, 0) AS is_self
    FROM Participant p
    LEFT JOIN Identity i ON p.identity_id = i.ROWID
    WHERE p.owner_id IN ({placeholders})
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, [int(item) for item in event_ids]).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        email = _safe_email(row["email"] or row["address"])
        name = _safe_text(row["display_name"] or email.split("@")[0], 120)
        item = {
            "name": name,
            "email_hash": _hash_text(email.lower()) if email else None,
            "domain": _safe_domain(email),
            "is_self": bool(row["is_self"]),
        }
        by_event.setdefault(str(row["event_rowid"]), []).append(item)
    return by_event


def _event_to_meeting(event: dict[str, Any], participants: list[dict[str, Any]]) -> dict[str, Any]:
    title = _safe_text(event.get("summary") or "Untitled meeting", 180)
    start_local = str(event.get("start_local") or "")
    end_local = str(event.get("end_local") or "")
    clue_tokens = _extract_clues([title, event.get("description") or "", event.get("location") or ""])
    participant_domains = sorted({p.get("domain") for p in participants if p.get("domain")})
    query_terms = sorted(set(_extract_clues([title]) + participant_domains + clue_tokens[:8]))
    meeting_key = stable_meeting_key(
        {
            "title": title,
            "start": start_local,
            "end": end_local,
            "participants": [p.get("email_hash") or p.get("domain") or p.get("name") for p in participants],
        }
    )
    return {
        "meeting_key": meeting_key,
        "calendar_event_ref": f"apple-calendar:event:{_hash_text(str(event.get('event_rowid')))}",
        "title": title,
        "start_local": start_local,
        "end_local": end_local,
        "date": start_local[:10],
        "calendar": _safe_text(event.get("calendar"), 120),
        "location": _safe_text(event.get("location"), 160),
        "participant_count": len(participants),
        "participants": participants[:20],
        "participant_domains": participant_domains[:20],
        "description_hash": _hash_text(event.get("description") or ""),
        "description_clues": clue_tokens[:12],
        "query_terms": query_terms[:20],
        "has_attendees": bool(event.get("has_attendees")) or bool(participants),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def stable_meeting_key(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return f"meeting:{_hash_text(raw)}"


def _skip_event(event: dict[str, Any]) -> bool:
    title = str(event.get("summary") or "").strip()
    if not title or title.startswith("Rocky:"):
        return True
    lowered = title.lower()
    if lowered in {"busy", "blocked", "focus", "tentative"}:
        return True
    return False


def _extract_clues(values: list[Any]) -> list[str]:
    text = " ".join(str(value or "") for value in values)
    text = SENSITIVE_RE.sub(" ", text)
    raw = re.findall(r"[A-Za-zÁ-ž0-9][A-Za-zÁ-ž0-9._-]{2,}", text)
    clues: list[str] = []
    for token in raw:
        norm = token.strip("._-").lower()
        if len(norm) < 3 or norm in STOPWORDS or norm.startswith("http"):
            continue
        if "@" in norm:
            domain = _safe_domain(norm)
            if domain:
                clues.append(domain)
        else:
            clues.append(norm[:48])
    seen: set[str] = set()
    unique: list[str] = []
    for clue in clues:
        if clue not in seen:
            unique.append(clue)
            seen.add(clue)
    return unique


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_email(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if "@" in text and not SENSITIVE_RE.search(text) else ""


def _safe_domain(email: Any) -> str | None:
    text = _safe_email(email)
    if "@" not in text:
        return None
    domain = text.rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in {"gmail.com", "icloud.com", "me.com"}:
        return domain or None
    return domain[:120]


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read sanitized upcoming Apple Calendar meetings.")
    parser.add_argument("--planning-date")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--db-path")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = read_upcoming_meetings(planning_date=args.planning_date, days=args.days, db_path=args.db_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting candidates: {payload.get('meeting_count', 0)}")
    return 0 if payload.get("status") in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
