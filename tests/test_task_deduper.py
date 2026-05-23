import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from task_deduper import dedupe_task_candidates


def test_deduper_merges_matching_dedupe_keys():
    payload = dedupe_task_candidates(
        [
            {"title": "Reply to investor", "dedupe_key": "task:x", "source_ref": "a", "confidence": 0.8},
            {"title": "Reply to investor", "dedupe_key": "task:x", "source_ref": "b", "confidence": 0.9},
        ]
    )

    assert payload["candidate_count"] == 1
    assert payload["duplicate_count"] == 1
    assert payload["candidates"][0]["confidence"] == 0.9
    assert payload["candidates"][0]["source_ref"] == "a,b"
