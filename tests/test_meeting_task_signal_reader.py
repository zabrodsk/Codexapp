import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from meeting_task_signal_reader import collect_meeting_task_signals


def test_meeting_action_sections_produce_dusan_task_signals(tmp_path):
    note = tmp_path / "meeting-test.md"
    note.write_text(
        """# meeting-test
---
title: Weekly sync
source_refs:
  - fireflies://transcript/abc
---

## Action items
**Dusan Zábrodský**
- Follow up with Eva about CRM access.

**Petr Smid**
- Prepare unrelated analysis.

## Transcript
Raw private transcript must not be copied.
""",
        encoding="utf-8",
    )

    payload = collect_meeting_task_signals(meeting_dir=tmp_path, since_days=14, now=datetime.now(timezone.utc))

    assert payload["status"] == "ok"
    assert payload["signal_count"] == 1
    signal = payload["signals"][0]
    assert signal["source"] == "Meeting"
    assert signal["source_ref"].startswith("obsidian-meeting:")
    assert "Follow up with Eva" in signal["summary"]
    assert "Raw private transcript" not in json.dumps(signal)


def test_meeting_reader_ignores_curation_boilerplate(tmp_path):
    note = tmp_path / "meeting-noise.md"
    note.write_text(
        """# Noise

## Action items captured
- Meeting ingestion: transcript-first source context captured for Rocky enrichment
- Recheck transcript source context
""",
        encoding="utf-8",
    )

    payload = collect_meeting_task_signals(meeting_dir=tmp_path, since_days=14, now=datetime.now(timezone.utc))

    assert payload["status"] == "ok"
    assert payload["signals"] == []
