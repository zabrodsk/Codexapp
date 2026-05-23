#!/usr/bin/env python3
"""Collect sanitized task signals from Rocky memory, meetings, and email."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from obsidian_memory import query_obsidian_vault


BETTY_ROOT = Path("/Users/clawdbot/.openclaw/workspace-betty")
BETTY_PYTHON = BETTY_ROOT / ".venv/bin/python"
BETTY_APPLE_MAIL_HELPER = BETTY_ROOT / "skills/apple-mail-control/scripts/apple_mail_helper.py"
DEFAULT_SINCE_DAYS = 7
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
TASK_MEMORY_QUERIES = [
    "open loop Dusan action follow up priority due",
    "tasks Dusan needs to do next step decision",
    "waiting on Dusan action item urgent",
]
TASK_MEETING_QUERIES = [
    "meeting action item Dusan follow up",
    "meeting notes Dusan promised next steps",
]


def collect_task_signals(
    *,
    sources: list[str] | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = 30,
    helper_payload: dict[str, Any] | None = None,
    memory_results: list[dict[str, Any]] | None = None,
    meeting_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = sources or ["email", "memory", "meetings"]
    signals: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if "email" in selected:
        try:
            signals.extend(collect_email_task_signals(since_days=since_days, limit=limit, helper_payload=helper_payload))
        except Exception as exc:
            errors.append({"source": "email", "reason": "email_signal_collection_failed", "error_hash": _hash_text(str(exc))})
    if "memory" in selected:
        try:
            signals.extend(collect_obsidian_task_signals(kind="memory", results=memory_results, limit=limit))
        except Exception as exc:
            errors.append({"source": "memory", "reason": "memory_signal_collection_failed", "error_hash": _hash_text(str(exc))})
    if "meetings" in selected:
        try:
            signals.extend(collect_obsidian_task_signals(kind="meetings", results=meeting_results, limit=limit))
        except Exception as exc:
            errors.append({"source": "meetings", "reason": "meeting_signal_collection_failed", "error_hash": _hash_text(str(exc))})
    return {
        "status": "ok" if not errors else "degraded",
        "signals": [_redact_signal(signal) for signal in signals[: max(1, int(limit))]],
        "signal_count": min(len(signals), max(1, int(limit))),
        "errors": errors,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def collect_email_task_signals(
    *,
    since_days: int = DEFAULT_SINCE_DAYS,
    limit: int = 30,
    helper_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if helper_payload is None:
        helper_payload = _invoke_betty_helper(hours=24 * max(1, int(since_days)), limit=limit)
    messages = {str(item.get("message_id") or ""): item for item in helper_payload.get("messages") or [] if isinstance(item, dict)}
    evaluations = [item for item in helper_payload.get("evaluations") or [] if isinstance(item, dict)]
    signals: list[dict[str, Any]] = []
    for item in evaluations:
        message_id = str(item.get("message_id") or "")
        if not item.get("important") or message_id not in messages:
            continue
        source_ref = f"apple-mail:message:{_hash_text(message_id)}"
        text = " ".join(
            part
            for part in [
                str(item.get("short_summary") or ""),
                str(item.get("importance_reason") or ""),
                str(item.get("awareness_point") or ""),
            ]
            if part
        )
        signals.append(
            {
                "signal_id": f"signal:{_hash_text(source_ref + text)}",
                "source": "Email",
                "source_ref": source_ref,
                "summary": _safe_text(text, 900),
                "priority_hint": _priority_from_betty(item.get("priority")),
                "requires_dusan_action_hint": True,
                "observed_at": helper_payload.get("checked_at") or _utc_now(),
                "evidence_hash": _hash_text(json.dumps({"source_ref": source_ref, "text": text}, sort_keys=True)),
                "untrusted": True,
            }
        )
    return signals


def collect_obsidian_task_signals(
    *,
    kind: str,
    results: list[dict[str, Any]] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    if results is None:
        queries = TASK_MEETING_QUERIES if kind == "meetings" else TASK_MEMORY_QUERIES
        results = []
        for query in queries:
            payload = query_obsidian_vault(query, limit=max(2, int(limit) // len(queries)), mode="query")
            if payload.get("status") == "ok":
                results.extend(payload.get("results") or [])
    source = "Meeting" if kind == "meetings" else "Memory"
    signals = []
    for item in results or []:
        path = str(item.get("path") or "")
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        if not (title or snippet):
            continue
        if _is_noise_memory_snippet(f"{title} {snippet}"):
            continue
        source_ref = f"obsidian:{_hash_text(path or title)}"
        signals.append(
            {
                "signal_id": f"signal:{_hash_text(source_ref + snippet)}",
                "source": source,
                "source_ref": source_ref,
                "summary": _safe_text(f"{title}: {snippet}", 900),
                "priority_hint": "normal",
                "requires_dusan_action_hint": _looks_actionable(snippet),
                "observed_at": _utc_now(),
                "evidence_hash": _hash_text(json.dumps({"path": path, "title": title, "snippet": snippet}, sort_keys=True)),
                "untrusted": True,
                "path": path,
            }
        )
    return signals


def build_manual_task_signal(text: str, *, source: str = "Command", source_ref: str = "manual:command") -> dict[str, Any]:
    safe = _safe_text(text, 900)
    return {
        "signal_id": f"signal:{_hash_text(source_ref + safe)}",
        "source": source,
        "source_ref": source_ref,
        "summary": safe,
        "priority_hint": "normal",
        "requires_dusan_action_hint": True,
        "observed_at": _utc_now(),
        "evidence_hash": _hash_text(safe),
        "untrusted": True,
    }


def _invoke_betty_helper(*, hours: int, limit: int) -> dict[str, Any]:
    proc = subprocess.run(
        [
            str(BETTY_PYTHON),
            str(BETTY_APPLE_MAIL_HELPER),
            "run",
            "--hours",
            str(max(1, int(hours))),
            "--limit",
            str(max(1, int(limit))),
        ],
        cwd=str(BETTY_ROOT),
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    if proc.returncode != 0:
        return {"status": "error", "error_hash": _hash_text(proc.stderr or proc.stdout)}
    return json.loads(proc.stdout)


def _looks_actionable(text: str) -> bool:
    if _is_noise_memory_snippet(text):
        return False
    return bool(re.search(r"\b(dusan|follow up|reply|send|review|decide|prepare|schedule|call|todo|action item|next step)\b", text, re.IGNORECASE))


def _is_noise_memory_snippet(text: str) -> bool:
    return bool(
        re.search(
            r"(@@|frontmatter|maintenance prompt|hand-maintained|DUSAN_PROFILE|agent collaboration rules|schema_version|last_updated)",
            text,
            re.IGNORECASE,
        )
    )


def _priority_from_betty(value: Any) -> str:
    raw = str(value or "").lower()
    if raw == "urgent":
        return "urgent"
    if raw == "soon":
        return "high"
    if raw == "waiting":
        return "normal"
    return "normal"


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{_hash_text(text)}]"
    return text[:limit]


def _redact_signal(signal: dict[str, Any]) -> dict[str, Any]:
    safe = dict(signal)
    for key in ["body", "content", "raw", "subject", "sender_email", "sender_name", "transcript"]:
        safe.pop(key, None)
    safe["summary"] = _safe_text(safe.get("summary"), 900)
    return safe


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
