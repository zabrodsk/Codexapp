import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from trainingpeaks_read_path_probe import probe_trainingpeaks_read_paths


def test_probe_works_when_no_trainingpeaks_source_is_configured(tmp_path):
    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=tmp_path / "missing-calendar.sqlite3",
        events_db_path=tmp_path / "missing-events.db",
        webcal_url_file=tmp_path / "missing-webcal-url",
        start_date="2026-05-25",
    )

    assert payload["status"] == "blocked"
    assert payload["recommendation"]["decision"] == "blocked_missing_trainingpeaks_source"
    assert payload["calendar_write_attempted"] is False


def test_probe_detects_candidate_apple_calendar_trainingpeaks_feed(tmp_path):
    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=tmp_path / "missing-calendar.sqlite3",
        events_db_path=tmp_path / "missing-events.db",
        webcal_url_file=tmp_path / "missing-webcal-url",
        start_date="2026-05-25",
        calendar_events=[
            {
                "summary": "Bike Endurance",
                "start_local": "2026-05-25 08:00:00",
                "end_local": "2026-05-25 09:30:00",
                "all_day": False,
                "calendar": "TrainingPeaks",
                "description": "coach notes should not leak",
            }
        ],
    )

    calendar_path = payload["paths"]["apple_calendar_subscribed_feed"]
    assert payload["recommendation"]["recommended_path"] == "apple_calendar_subscribed_feed"
    assert calendar_path["status"] == "available"
    assert calendar_path["candidate_count"] == 1
    rendered = json.dumps(payload)
    assert "coach notes should not leak" not in rendered


def test_probe_recommends_secret_webcal_file_with_safe_permissions(tmp_path):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    url_file = secret_dir / "trainingpeaks-webcal-url"
    url_file.write_text("webcal://example.test/private-feed.ics", encoding="utf-8")
    url_file.chmod(0o600)

    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=tmp_path / "missing-calendar.sqlite3",
        events_db_path=tmp_path / "missing-events.db",
        webcal_url_file=url_file,
        start_date="2026-05-25",
        calendar_events=[],
    )

    assert payload["recommendation"]["recommended_path"] == "direct_ics_webcal_url_file"
    rendered = json.dumps(payload)
    assert "private-feed" not in rendered


def test_probe_blocks_secret_webcal_file_with_open_permissions(tmp_path):
    url_file = tmp_path / "trainingpeaks-webcal-url"
    url_file.write_text("webcal://example.test/private-feed.ics", encoding="utf-8")
    url_file.chmod(0o644)

    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=tmp_path / "missing-calendar.sqlite3",
        events_db_path=tmp_path / "missing-events.db",
        webcal_url_file=url_file,
        start_date="2026-05-25",
        calendar_events=[],
    )

    assert payload["recommendation"]["decision"] == "blocked_until_secret_permissions_are_fixed"
    assert "webcal_url_file_permissions_too_open" in payload["paths"]["direct_ics_webcal_url_file"]["permission_warnings"]


def test_probe_summarizes_whoop_events_without_raw_payload(tmp_path):
    events_db = tmp_path / "events.db"
    with sqlite3.connect(events_db) as conn:
        conn.execute("CREATE TABLE events (source TEXT, start_ts_utc INTEGER, payload TEXT)")
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            ("whoop", 1770000000, '{"secret":"raw whoop payload"}'),
        )

    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=tmp_path / "missing-calendar.sqlite3",
        events_db_path=events_db,
        webcal_url_file=tmp_path / "missing-webcal-url",
        start_date="2026-05-25",
        calendar_events=[],
    )

    whoop = payload["paths"]["whoop_timing_evidence"]
    assert whoop["status"] == "available"
    assert whoop["event_count"] == 1
    assert whoop["use"] == "timing_sanity_check_only"
    assert "raw whoop payload" not in json.dumps(payload)
