#!/usr/bin/env python3
"""Sanitized unread-email attention reader for Rocky email triage booking."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


BETTY_ROOT = Path("/Users/clawdbot/.openclaw/workspace-betty")
BETTY_PYTHON = BETTY_ROOT / ".venv/bin/python"
BETTY_APPLE_MAIL_HELPER = BETTY_ROOT / "skills/apple-mail-control/scripts/apple_mail_helper.py"
DEFAULT_HOURS = 168
DEFAULT_LIMIT = 100
UNSAFE_TEXT_RE = re.compile(
    r"(webcal://|https?://|cookie|token|secret|password|credential|auth|Bearer\s+|sk-)",
    re.IGNORECASE,
)
SENSITIVE_KEYS_RE = re.compile(
    r"(body|content|description|html|notes|password|preview|raw|secret|subject|token|transcript)",
    re.IGNORECASE,
)


def collect_email_attention(
    *,
    hours: int = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    helper_payload: dict[str, Any] | None = None,
    helper_path: str | Path = BETTY_APPLE_MAIL_HELPER,
    python_path: str | Path = BETTY_PYTHON,
) -> dict[str, Any]:
    """Run Betty's raw helper and return only safe counts, hashes, and refs."""
    if helper_payload is None:
        helper_payload = _invoke_betty_helper(
            hours=hours,
            limit=limit,
            helper_path=helper_path,
            python_path=python_path,
        )
    if helper_payload.get("status") not in {"ok", None} and helper_payload.get("source") == "blocked":
        return _blocked("apple_mail_read_failed", helper_payload)
    if helper_payload.get("status") not in {"ok", None} and "error" in helper_payload:
        return _blocked("email_attention_evaluation_failed", helper_payload)

    messages = [item for item in helper_payload.get("messages") or [] if isinstance(item, dict)]
    evaluations = [item for item in helper_payload.get("evaluations") or [] if isinstance(item, dict)]
    by_id = {str(item.get("message_id") or ""): item for item in messages}
    important = [
        item for item in evaluations
        if item.get("important") is True and str(item.get("message_id") or "") in by_id
    ]
    priority_buckets: dict[str, int] = {}
    source_refs: list[str] = []
    for item in important:
        priority = _safe_priority(item.get("priority"))
        priority_buckets[priority] = priority_buckets.get(priority, 0) + 1
        source_refs.append(_source_ref(str(item.get("message_id") or "")))

    evidence_basis = {
        "hours": int(hours),
        "limit": int(limit),
        "unread_count": len(messages),
        "attention_count": len(important),
        "priority_buckets": priority_buckets,
        "source_refs": sorted(source_refs),
    }
    evidence_hash = _hash_json(evidence_basis)
    return _redact_payload(
        {
            "status": "ok",
            "source": "apple-mail-native",
            "hours": int(hours),
            "limit": int(limit),
            "unread_count": len(messages),
            "evaluated_count": len(evaluations),
            "attention_count": len(important),
            "priority_buckets": priority_buckets,
            "source_refs": source_refs,
            "evidence_hash": evidence_hash,
            "checked_at": helper_payload.get("checked_at"),
            "calendar_write_attempted": False,
            "raw_fields_excluded": [
                "subject",
                "sender",
                "preview",
                "body_excerpt",
                "report",
            ],
        }
    )


def _invoke_betty_helper(
    *,
    hours: int,
    limit: int,
    helper_path: str | Path,
    python_path: str | Path,
) -> dict[str, Any]:
    helper = Path(helper_path)
    python = Path(python_path)
    if not python.exists():
        raise RuntimeError(f"Betty python missing at {python}")
    if not helper.exists():
        raise RuntimeError(f"Betty Apple Mail helper missing at {helper}")
    proc = subprocess.run(
        [
            str(python),
            str(helper),
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
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        payload.setdefault("status", "error")
        payload.setdefault("error", proc.stderr or f"Betty helper exited {proc.returncode}")
        return payload
    return json.loads(proc.stdout)


def _blocked(reason: str, helper_payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_payload(
        {
            "status": "blocked",
            "reason": reason,
            "source": helper_payload.get("source"),
            "error_hash": _hash_text(str(helper_payload.get("error") or helper_payload.get("reason") or reason)),
            "calendar_write_attempted": False,
        }
    )


def _source_ref(message_id: str) -> str:
    return f"apple-mail:message:{_hash_text(message_id)}"


def _safe_priority(value: Any) -> str:
    priority = str(value or "later").strip().lower()
    if priority not in {"urgent", "soon", "later", "waiting", "ignore"}:
        return "later"
    return priority


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact_payload(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and SENSITIVE_KEYS_RE.search(parent_key):
        return {"redacted": True, "sha256": _hash_text(json.dumps(value, sort_keys=True, default=str)), "chars": len(str(value))}
    if isinstance(value, dict):
        return {str(key): _redact_payload(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and UNSAFE_TEXT_RE.search(value):
        return {"redacted": True, "sha256": _hash_text(value), "chars": len(value)}
    return value
