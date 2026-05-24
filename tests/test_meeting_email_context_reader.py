import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_email_context_reader import collect_meeting_email_context


def test_mail_context_reads_metadata_without_mutation_or_body(tmp_path):
    db = tmp_path / "Envelope Index"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, sender INTEGER, subject INTEGER, summary INTEGER, date_received INTEGER, deleted INTEGER, read INTEGER);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE summaries (ROWID INTEGER PRIMARY KEY, summary TEXT);
        INSERT INTO addresses VALUES (1, 'jana@example.com', 'Jana');
        INSERT INTO subjects VALUES (1, 'Portfolio update');
        INSERT INTO summaries VALUES (1, 'Quick summary only, not a raw body.');
        INSERT INTO messages VALUES (10, 1, 1, 1, 2000000000, 0, 0);
        """
    )
    conn.commit()
    conn.close()

    payload = collect_meeting_email_context(
        {"title": "Jana portfolio", "participant_domains": ["example.com"], "query_terms": ["portfolio"]},
        mail_db_path=db,
    )

    assert payload["status"] == "ok"
    assert payload["item_count"] == 1
    assert payload["items"][0]["source_ref"].startswith("apple-mail:message:")
    assert payload["items"][0]["sender_domain"] == "example.com"
    assert "raw body" in payload["items"][0]["summary_preview"]
    assert payload["calendar_write_attempted"] is False
