import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_calendar_reader import APPLE_EPOCH_OFFSET, read_upcoming_meetings


def _apple_ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp()) - APPLE_EPOCH_OFFSET


def _calendar_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE CalendarItem (
          ROWID INTEGER PRIMARY KEY,
          summary TEXT,
          start_date INTEGER,
          end_date INTEGER,
          all_day INTEGER,
          calendar_id INTEGER,
          description TEXT,
          has_attendees INTEGER
        );
        CREATE TABLE Calendar (ROWID INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE Location (item_owner_id INTEGER, title TEXT, address TEXT);
        CREATE TABLE Participant (owner_id INTEGER, identity_id INTEGER, email TEXT, is_self INTEGER);
        CREATE TABLE Identity (ROWID INTEGER PRIMARY KEY, display_name TEXT, address TEXT, first_name TEXT, last_name TEXT);
        INSERT INTO Calendar VALUES (1, 'Calendar');
        INSERT INTO Location VALUES (10, 'Office', '');
        INSERT INTO Identity VALUES (2, 'Jana Novak', 'jana@example.com', 'Jana', 'Novak');
        INSERT INTO Participant VALUES (10, 2, 'jana@example.com', 0);
        """
    )
    conn.execute(
        "INSERT INTO CalendarItem VALUES (10, 'Board prep with Jana', ?, ?, 0, 1, 'Discuss Rockaway portfolio runway. token=secret', 1)",
        (_apple_ts("2026-05-25T10:00:00"), _apple_ts("2026-05-25T11:00:00")),
    )
    conn.execute(
        "INSERT INTO CalendarItem VALUES (11, 'Rocky: Coding focus - X', ?, ?, 0, 1, '', 0)",
        (_apple_ts("2026-05-25T12:00:00"), _apple_ts("2026-05-25T13:00:00")),
    )
    conn.commit()
    conn.close()


def test_reads_meetings_with_participants_without_raw_description(tmp_path):
    db = tmp_path / "Calendar.sqlitedb"
    _calendar_db(db)

    payload = read_upcoming_meetings(planning_date="2026-05-25", db_path=db)

    assert payload["status"] == "ok"
    assert payload["meeting_count"] == 1
    meeting = payload["meetings"][0]
    assert meeting["title"] == "Board prep with Jana"
    assert meeting["participant_domains"] == ["example.com"]
    assert meeting["participants"][0]["name"] == "Jana Novak"
    assert "description" not in meeting
    assert "secret" not in str(meeting)
    assert meeting["calendar_write_attempted"] is False
