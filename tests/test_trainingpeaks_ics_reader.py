import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from trainingpeaks_ics_reader import preview_ics_file, preview_ics_text, preview_webcal_url_file


def test_parses_timed_workout_with_start_and_end():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-1
SUMMARY:Bike Endurance
DTSTART;TZID=Europe/Prague:20260525T080000
DTEND;TZID=Europe/Prague:20260525T093000
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
        observed_at="2026-05-22T12:00:00+02:00",
    )

    workout = payload["workouts"][0]
    assert payload["status"] == "ok"
    assert workout["date"] == "2026-05-25"
    assert workout["planned_start_local"] == "2026-05-25T08:00:00+02:00"
    assert workout["planned_end_local"] == "2026-05-25T09:30:00+02:00"
    assert workout["planned_duration_minutes"] == 90
    assert workout["sport"] == "bike"
    assert workout["confidence"] == "high"


def test_parses_duration_only_workout():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-2
SUMMARY:Run Tempo
DTSTART;TZID=Europe/Prague:20260525T081500
DURATION:PT1H15M
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
    )

    workout = payload["workouts"][0]
    assert workout["planned_end_local"] == "2026-05-25T09:30:00+02:00"
    assert workout["planned_duration_minutes"] == 75
    assert workout["sport"] == "run"


def test_handles_untimed_all_day_workout_as_low_confidence():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-3
SUMMARY:Strength
DTSTART;VALUE=DATE:20260525
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
    )

    workout = payload["workouts"][0]
    assert workout["date"] == "2026-05-25"
    assert workout["planned_start_local"] is None
    assert workout["planned_end_local"] is None
    assert workout["planned_duration_minutes"] is None
    assert workout["confidence"] == "low"
    assert "untimed_or_all_day_workout" in workout["warnings"]


def test_handles_folded_ics_lines():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-4
SUMMARY:Long bike
  endurance ride
DTSTART;TZID=Europe/Prague:20260525T080000
DTEND;TZID=Europe/Prague:20260525T100000
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
    )

    assert payload["workouts"][0]["title"] == "Long bike endurance ride"


def test_skips_recurring_events_instead_of_guessing():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-5
SUMMARY:Bike recurring
DTSTART;TZID=Europe/Prague:20260525T080000
DTEND;TZID=Europe/Prague:20260525T090000
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
    )

    assert payload["workout_count"] == 0
    assert payload["skipped_count"] == 1
    assert "unsupported_recurring_workout" in payload["warnings"]


def test_redacts_descriptions_and_auth_like_titles():
    payload = preview_ics_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-6
SUMMARY:Auth token workout
DESCRIPTION:cookie=abc token=def password=ghi
DTSTART;TZID=Europe/Prague:20260525T080000
DTEND;TZID=Europe/Prague:20260525T090000
END:VEVENT
END:VCALENDAR
""",
        start_date="2026-05-25",
    )

    rendered = json.dumps(payload)
    assert "cookie=abc" not in rendered
    assert "token=def" not in rendered
    assert payload["workouts"][0]["title"] == "Planned workout"


def test_preview_ics_file_reads_fixture(tmp_path):
    fixture = tmp_path / "trainingpeaks.ics"
    fixture.write_text(
        """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:tp-7
SUMMARY:Swim Technique
DTSTART;TZID=Europe/Prague:20260525T070000
DTEND;TZID=Europe/Prague:20260525T080000
END:VEVENT
END:VCALENDAR
""",
        encoding="utf-8",
    )

    payload = preview_ics_file(fixture, start_date="2026-05-25")
    assert payload["workouts"][0]["sport"] == "swim"
    assert payload["input"]["type"] == "ics_file"


def test_webcal_url_file_output_never_contains_secret_url(tmp_path, monkeypatch):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    url_file = secret_dir / "trainingpeaks-webcal-url"
    url_file.write_text("webcal://example.test/private-token.ics", encoding="utf-8")
    url_file.chmod(0o600)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"

    monkeypatch.setattr("trainingpeaks_ics_reader.urlopen", lambda *args, **kwargs: Response())
    payload = preview_webcal_url_file(url_file, start_date="2026-05-25")
    rendered = json.dumps(payload)
    assert "private-token" not in rendered
    assert payload["input"]["url_redacted"] is True
