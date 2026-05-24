#!/usr/bin/env python3
"""Read completed meeting notes as sanitized outcome candidates."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_MEETING_DIR = Path("/Users/clawdbot/Documents/VAULT/Rocky/OpenClaw Memory/meetings")
DEFAULT_SINCE_DAYS = 7
OUTCOME_HEADING_RE = re.compile(
    r"^##+\s*(?P<title>outcomes?|decisions?|follow[- ]?ups?|next steps?|action items?|commitments?|relationship notes?|investor notes?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
HEADING_RE = re.compile(r"^##+\s+", re.MULTILINE)
SENSITIVE_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def collect_meeting_outcome_candidates(
    *,
    meeting_dir: str | Path | None = DEFAULT_MEETING_DIR,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return recent structured meeting outcome candidates.

    This intentionally reads structured sections only. It does not copy raw
    transcript bodies into state, logs, or command output.
    """
    root = Path(meeting_dir or DEFAULT_MEETING_DIR).expanduser()
    if not root.exists():
        return {
            "status": "blocked",
            "reason": "meeting_directory_missing",
            "meeting_dir": str(root),
            "candidates": [],
            "candidate_count": 0,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = now_dt - timedelta(days=max(1, int(since_days)))
    files = [
        path
        for path in root.glob("*.md")
        if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) >= cutoff
    ]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in files:
        try:
            candidate = _candidate_from_file(path, observed_at=now_dt.isoformat())
            if candidate:
                candidates.append(candidate)
        except Exception as exc:
            warnings.append({"path_hash": _hash_text(str(path)), "reason": "meeting_outcome_file_read_failed", "error_hash": _hash_text(str(exc))})
        if len(candidates) >= max(1, int(limit)):
            break
    return _redact(
        {
            "status": "ok" if not warnings else "degraded",
            "reason": None if not warnings else "some_meeting_outcome_files_failed",
            "meeting_dir": str(root),
            "candidates": candidates[: max(1, int(limit))],
            "candidate_count": len(candidates[: max(1, int(limit))]),
            "warning_count": len(warnings),
            "warnings": warnings,
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    )


def _candidate_from_file(path: Path, *, observed_at: str) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = _extract_structured_sections(text)
    if not sections:
        return None
    title = _extract_title(text, path)
    meeting_date = _extract_date(text, path)
    source_refs = _extract_source_refs(text)
    structured_lines = []
    for section in sections:
        for line in section["lines"]:
            structured_lines.append({"section": section["heading"], "text": line})
    path_hash = _hash_text(str(path))
    meeting_key = _extract_meeting_key(text) or f"meeting-outcome:{meeting_date or 'unknown'}:{_hash_text(title + path_hash)}"
    evidence_payload = {"path": str(path), "title": title, "meeting_date": meeting_date, "lines": structured_lines}
    return {
        "meeting_key": meeting_key,
        "source": "Obsidian meeting note",
        "source_ref": f"obsidian-meeting-outcome:{path_hash}",
        "path": str(path),
        "path_hash": path_hash,
        "title": _safe_text(title, 180),
        "meeting_date": meeting_date,
        "observed_at": observed_at,
        "structured_sections": sections,
        "structured_lines": structured_lines[:80],
        "structured_line_count": len(structured_lines),
        "source_refs": source_refs[:10],
        "evidence_hash": _hash_text(json.dumps(evidence_payload, sort_keys=True, default=str)),
        "untrusted_control_input": True,
    }


def _extract_structured_sections(text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for match in OUTCOME_HEADING_RE.finditer(text):
        start = match.end()
        next_heading = HEADING_RE.search(text, start)
        end = next_heading.start() if next_heading else len(text)
        lines = [_normalize_line(line) for line in text[start:end].splitlines()]
        safe_lines = [line for line in lines if line and not _is_noise(line)]
        if safe_lines:
            sections.append({"heading": _safe_text(match.group("title"), 80), "lines": safe_lines[:30]})
    return sections


def _normalize_line(line: str) -> str:
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
    text = re.sub(r"^\[[ xX]\]\s+", "", text.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return _safe_text(text, 500)


def _is_noise(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) < 5:
        return True
    return bool(re.search(r"(source context captured|transcript-first|smarty curation|frontmatter)", stripped, re.IGNORECASE))


def _extract_title(text: str, path: Path) -> str:
    match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip().strip('"')
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return path.stem


def _extract_date(text: str, path: Path) -> str | None:
    for pattern in [r"^date:\s*(20\d{2}-\d{2}-\d{2})", r"(20\d{2}-\d{2}-\d{2})"]:
        match = re.search(pattern, text if pattern.startswith("^") else path.name, re.MULTILINE)
        if match:
            return match.group(1)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime.date().isoformat()


def _extract_meeting_key(text: str) -> str | None:
    match = re.search(r"^meeting[_ -]?key:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return _safe_text(match.group(1), 180) if match else None


def _extract_source_refs(text: str) -> list[str]:
    refs: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- fireflies://") or stripped.startswith("- granola://"):
            refs.append(stripped[2:].strip())
        elif stripped.lower().startswith("fireflies:"):
            refs.append(f"fireflies:{_hash_text(stripped)}")
    return refs


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 1000)
    return value


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
