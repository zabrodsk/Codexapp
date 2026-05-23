#!/usr/bin/env python3
"""Direct meeting-note task signal reader for Rocky.

The generic Obsidian query path is useful for context, but meeting notes already
contain structured action sections. This reader inspects those sections directly
and emits sanitized task signals without copying transcript bodies.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_MEETING_DIR = Path("/Users/clawdbot/Documents/VAULT/Rocky/OpenClaw Memory/meetings")
DEFAULT_SINCE_DAYS = 14
ACTION_HEADING_RE = re.compile(
    r"^##+\s*(action items?|action items captured|actions?|next steps?|decisions?\s*/\s*commitments?|commitments?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
HEADING_RE = re.compile(r"^##+\s+", re.MULTILINE)
OWNER_RE = re.compile(r"^\s*\*\*([^*]{2,120})\*\*\s*$")
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
CHECKBOX_RE = re.compile(r"^\s*\[[ xX]\]\s+")
OWNER_ACTION_RE = re.compile(r"^\s*(?P<owner>(?:\[\[[^\]]+\]\]|[A-ZÁ-Ž][^:–—-]{1,80}?))\s*(?::|[-–—])\s*(?P<action>\S.{4,})$")
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(no action|none\b|\bn/?a\b|recheck transcript|source context captured|meeting ingestion|"
    r"transcript-first|smarty curation|frontmatter|keywords?\b|durable context\b)",
    re.IGNORECASE,
)
ACTION_VERB_RE = re.compile(
    r"\b(follow up|reply|send|review|decide|prepare|schedule|call|book|draft|finish|"
    r"check|confirm|coordinate|organize|provide|share|update|create|resolve|chase|"
    r"připravit|komunikovat|aktualizovat|vyvinout|řešit|koordinovat|provést|přebrat|"
    r"poskytovat|navrhnout|organizovat)\b",
    re.IGNORECASE,
)


def collect_meeting_task_signals(
    *,
    meeting_dir: str | Path = DEFAULT_MEETING_DIR,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read recent meeting notes and return sanitized task signals."""
    root = Path(meeting_dir).expanduser()
    if not root.exists():
        return {
            "status": "blocked",
            "reason": "meeting_directory_missing",
            "signals": [],
            "signal_count": 0,
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

    signals: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in files:
        try:
            signals.extend(_signals_from_file(path, observed_at=now_dt.isoformat()))
        except Exception as exc:
            warnings.append({"path_hash": _hash_text(str(path)), "reason": "meeting_file_read_failed", "error_hash": _hash_text(str(exc))})
        if len(signals) >= max(1, int(limit)):
            break
    limited = signals[: max(1, int(limit))]
    return {
        "status": "ok" if not warnings else "degraded",
        "reason": None if not warnings else "some_meeting_files_failed",
        "signals": limited,
        "signal_count": len(limited),
        "warning_count": len(warnings),
        "warnings": warnings,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _signals_from_file(path: Path, *, observed_at: str) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    title = _extract_title(text, path)
    source_refs = _extract_source_refs(text)
    sections = _extract_action_sections(text)
    signals: list[dict[str, Any]] = []
    for section_index, section in enumerate(sections):
        owner = "Unknown"
        for raw_line in section.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            owner_match = OWNER_RE.match(line)
            if owner_match:
                owner = owner_match.group(1).strip()
                continue
            if _looks_owner_line(line) and not LIST_MARKER_RE.match(line):
                owner = line.strip().strip(":")
                continue
            action = _normalize_action_line(line)
            parsed_owner, parsed_action = _split_owner_action(action)
            effective_owner = parsed_owner or owner
            if parsed_action:
                action = parsed_action
            if not action or _is_noise(action) or _looks_owner_line(action):
                continue
            requires_dusan = _is_dusan(effective_owner) or _mentions_dusan(action)
            if not requires_dusan and effective_owner != "Unknown":
                continue
            if not requires_dusan and not ACTION_VERB_RE.search(action):
                continue
            source_ref = f"obsidian-meeting:{_hash_text(str(path))}:action:{_hash_text(str(section_index) + action)}"
            summary = _safe_text(f"{title}: {effective_owner if effective_owner != 'Unknown' else 'Action'} - {action}", 900)
            signals.append(
                {
                    "signal_id": f"signal:{_hash_text(source_ref + summary)}",
                    "source": "Meeting",
                    "source_ref": source_ref,
                    "summary": summary,
                    "priority_hint": "normal",
                    "requires_dusan_action_hint": bool(requires_dusan),
                    "observed_at": observed_at,
                    "evidence_hash": _hash_text(json.dumps({"path": str(path), "title": title, "action": action}, sort_keys=True)),
                    "untrusted": True,
                    "path": str(path),
                    "meeting_title": _safe_text(title, 180),
                    "owner_hint": _safe_text(effective_owner, 120),
                    "meeting_source_refs": source_refs[:5],
                }
            )
    return signals


def _extract_action_sections(text: str) -> list[str]:
    sections: list[str] = []
    for match in ACTION_HEADING_RE.finditer(text):
        if _inside_smarty_curation(text, match.start()):
            continue
        start = match.end()
        next_heading = HEADING_RE.search(text, start)
        end = next_heading.start() if next_heading else len(text)
        section = text[start:end].strip()
        if section:
            sections.append(section)
    return sections


def _inside_smarty_curation(text: str, position: int) -> bool:
    previous = list(re.finditer(r"^##\s+(.+)$", text[:position], re.MULTILINE))
    if not previous:
        return False
    heading = previous[-1].group(1).strip().lower()
    return heading.startswith("smarty curation")


def _extract_title(text: str, path: Path) -> str:
    match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip().strip('"')
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return path.stem


def _extract_source_refs(text: str) -> list[str]:
    refs: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- fireflies://") or stripped.startswith("- granola://"):
            refs.append(stripped[2:].strip())
        elif stripped.startswith("Fireflies:"):
            refs.append(f"fireflies:{_hash_text(stripped)}")
    return refs


def _normalize_action_line(line: str) -> str:
    line = LIST_MARKER_RE.sub("", line).strip()
    line = CHECKBOX_RE.sub("", line).strip()
    line = re.sub(r"\s+", " ", line)
    return line.strip("-* ")


def _split_owner_action(text: str) -> tuple[str | None, str | None]:
    match = OWNER_ACTION_RE.match(str(text or "").strip())
    if not match:
        return None, None
    owner = match.group("owner").strip().strip(":")
    action = match.group("action").strip()
    if ACTION_VERB_RE.search(owner) or not ACTION_VERB_RE.search(action):
        return None, None
    owner = owner.strip("[]") if owner.startswith("[[") and owner.endswith("]]" ) else owner
    return owner, action


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 8:
        return True
    if re.match(r"^\*\*[^*]+\*\*\s*[-:–—]", stripped) and not ACTION_VERB_RE.search(stripped):
        return True
    if re.match(r"^[-*+]\s+\*\*[^*]+\*\*\s*[-:–—]", stripped) and not ACTION_VERB_RE.search(stripped):
        return True
    return bool(NOISE_RE.search(stripped))


def _looks_owner_line(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip().strip(":")
    if not cleaned or len(cleaned) > 80:
        return False
    if _is_dusan(cleaned):
        return True
    if ACTION_VERB_RE.search(cleaned):
        return False
    return bool(re.fullmatch(r"(?:\[\[[^\]]+\]\]|[A-ZÁ-Ž][A-Za-zÁ-ž.-]+)(?:\s+(?:\[\[[^\]]+\]\]|[A-ZÁ-Ž][A-Za-zÁ-ž.-]+)){0,3}", cleaned))


def _is_dusan(value: str) -> bool:
    normalized = _ascii_fold(value).lower()
    return "dusan" in normalized or "zabrod" in normalized


def _mentions_dusan(value: str) -> bool:
    return _is_dusan(value)


def _ascii_fold(value: str) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{_hash_text(text)}]"
    return text[:limit]


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read sanitized task signals from meeting action sections.")
    parser.add_argument("--meeting-dir", default=str(DEFAULT_MEETING_DIR))
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    payload = collect_meeting_task_signals(meeting_dir=args.meeting_dir, since_days=args.since_days, limit=args.limit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting task signals: {payload.get('status')} ({payload.get('signal_count', 0)} signals)")
    return 0 if payload.get("status") in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
