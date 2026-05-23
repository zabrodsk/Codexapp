#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


DEFAULT_DB_PATH = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
APPLE_EPOCH_OFFSET = 978307200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read Apple Calendar events from the local Calendar SQLite database."
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to include from the start date. Default: 1.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Calendar database path. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--include-all-day",
        action="store_true",
        help="Include all-day events in the output.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Output format. Default: text.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        start = datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("Invalid --date. Expected YYYY-MM-DD.", file=sys.stderr)
        return 2

    if args.days < 1:
        print("--days must be >= 1.", file=sys.stderr)
        return 2

    db_path = Path(args.db_path).expanduser()
    if not db_path.exists():
        print(f"Calendar DB not found: {db_path}", file=sys.stderr)
        return 1

    end = start + timedelta(days=args.days)
    try:
        events = query_events(
            db_path=db_path,
            start=start,
            end=end,
            include_all_day=args.include_all_day,
        )
    except sqlite3.Error as exc:
        print(f"Calendar DB query failed: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(events, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        render_csv(events)
    else:
        print(render_text(events, start=start, end=end))
    return 0


def query_events(
    *,
    db_path: Path,
    start: datetime,
    end: datetime,
    include_all_day: bool,
) -> list[dict[str, Any]]:
    query = """
    SELECT
      COALESCE(ci.summary, '') AS summary,
      datetime(ci.start_date + ?, 'unixepoch', 'localtime') AS start_local,
      datetime(ci.end_date + ?, 'unixepoch', 'localtime') AS end_local,
      COALESCE(ci.all_day, 0) AS all_day,
      COALESCE(c.title, '') AS calendar,
      TRIM(COALESCE(loc.title, '') || CASE
        WHEN loc.address IS NOT NULL AND loc.address != '' AND loc.title IS NOT NULL AND loc.title != '' THEN ' | '
        ELSE ''
      END || COALESCE(loc.address, '')) AS location,
      COALESCE(ci.description, '') AS description
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

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for row in rows:
        event = {
            "summary": compact(row["summary"]),
            "start_local": row["start_local"],
            "end_local": row["end_local"],
            "all_day": bool(row["all_day"]),
            "calendar": compact(row["calendar"]),
            "location": compact(row["location"]),
            "description": compact(row["description"]),
        }
        if event["all_day"] and not include_all_day:
            continue
        events.append(event)
    return events


def compact(value: str) -> str:
    return " ".join(value.split())


def render_csv(events: list[dict[str, Any]]) -> None:
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "summary",
            "start_local",
            "end_local",
            "all_day",
            "calendar",
            "location",
            "description",
        ],
    )
    writer.writeheader()
    writer.writerows(events)


def render_text(events: list[dict[str, Any]], *, start: datetime, end: datetime) -> str:
    header = f"Apple Calendar events from {start.strftime('%Y-%m-%d')} to {(end - timedelta(seconds=1)).strftime('%Y-%m-%d')}"
    if not events:
        return f"{header}\nNo events found."

    lines = [header]
    for event in events:
        if event["all_day"]:
            when = "All day"
        else:
            when = f"{event['start_local']} -> {event['end_local']}"
        lines.append(f"- {when} | {event['summary'] or '(No title)'}")
        if event["calendar"]:
            lines.append(f"  Calendar: {event['calendar']}")
        if event["location"]:
            lines.append(f"  Location: {event['location']}")
        if event["description"]:
            lines.append(f"  Notes: {event['description']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
