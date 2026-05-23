from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LESSONS_PATH = ROOT / "improvement" / "lessons.jsonl"
MAX_TEXT_LENGTH = 280
ALLOWED_STATUS = {"candidate", "validated", "rejected"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bntn_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bapify_api_[A-Za-z0-9]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
]


@dataclass(frozen=True)
class LessonRecord:
    created_at: str
    workflow: str
    symptom: str
    root_cause: str
    safer_future_behavior: str
    status: str = "candidate"
    confidence: str = "medium"
    source_ref: str | None = None
    tags: list[str] | None = None


def capture_lesson(
    *,
    workflow: str,
    symptom: str,
    root_cause: str,
    safer_future_behavior: str,
    status: str = "candidate",
    confidence: str = "medium",
    source_ref: str | None = None,
    tags: list[str] | None = None,
    lessons_path: Path = DEFAULT_LESSONS_PATH,
) -> dict[str, Any]:
    record = LessonRecord(
        created_at=datetime.now(timezone.utc).isoformat(),
        workflow=_sanitize_required("workflow", workflow),
        symptom=_sanitize_required("symptom", symptom),
        root_cause=_sanitize_required("root_cause", root_cause),
        safer_future_behavior=_sanitize_required("safer_future_behavior", safer_future_behavior),
        status=_validate_choice("status", status, ALLOWED_STATUS),
        confidence=_validate_choice("confidence", confidence, ALLOWED_CONFIDENCE),
        source_ref=_sanitize_optional("source_ref", source_ref),
        tags=_sanitize_tags(tags or []),
    )
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    with lessons_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    return {"status": "ok", "path": str(lessons_path), "lesson": asdict(record)}


def read_recent_lessons(limit: int = 10, *, lessons_path: Path = DEFAULT_LESSONS_PATH) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    if not lessons_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with lessons_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows[-limit:][::-1]


def format_recent_lessons(lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return "No lessons recorded yet."
    lines = [f"Recent Lessons (latest {len(lessons)})", "=" * 60]
    for lesson in lessons:
        stamp = str(lesson.get("created_at") or "?")[:19]
        workflow = lesson.get("workflow") or "unknown-workflow"
        status = lesson.get("status") or "candidate"
        lines.append(f"[{status}] {workflow} [{stamp}]")
        lines.append(f"  symptom: {lesson.get('symptom', '')}")
        lines.append(f"  fix: {lesson.get('safer_future_behavior', '')}")
        if lesson.get("source_ref"):
            lines.append(f"  source: {lesson['source_ref']}")
    return "\n".join(lines)


def _sanitize_required(field_name: str, value: str) -> str:
    cleaned = _compact_text(value)
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    _reject_secret_like(field_name, cleaned)
    return cleaned


def _sanitize_optional(field_name: str, value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _compact_text(value)
    if not cleaned:
        return None
    _reject_secret_like(field_name, cleaned)
    return cleaned


def _sanitize_tags(tags: list[str]) -> list[str]:
    cleaned_tags: list[str] = []
    for tag in tags:
        cleaned = _compact_text(tag, max_length=40).lower().replace(" ", "-")
        cleaned = re.sub(r"[^a-z0-9._-]+", "", cleaned)
        if cleaned and cleaned not in cleaned_tags:
            cleaned_tags.append(cleaned)
    return cleaned_tags[:6]


def _validate_choice(field_name: str, value: str, choices: set[str]) -> str:
    cleaned = _compact_text(value, max_length=20).lower()
    if cleaned not in choices:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(choices))}")
    return cleaned


def _compact_text(value: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    collapsed = re.sub(r"\s+", " ", str(value or "").strip())
    return collapsed[:max_length].strip()


def _reject_secret_like(field_name: str, value: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"{field_name} looks like it contains a secret; summarize it instead")
