#!/usr/bin/env python3
"""Durable source-level ledger for Rocky task commands."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER_DB = Path("/Users/clawdbot/.openclaw/workspace/improvement/task_command_ledger.sqlite3")
VALID_STATUSES = {"seen", "applied", "skipped_duplicate", "blocked", "manual_review_required", "ack_sent", "ack_failed"}
SENSITIVE_TEXT_RE = re.compile(
    r"(https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"cookie|token|secret|password|credential|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def safe_preview(value: Any, *, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{hash_text(text)}]"
    return text[:limit]


def command_fingerprint(command: dict[str, Any]) -> str:
    raw = {
        "source": command.get("source_channel") or command.get("source") or "Command",
        "source_ref": command.get("source_ref") or "",
        "text_hash": hash_text(command.get("text") or command.get("summary") or ""),
    }
    return f"task-command:{hash_text(json.dumps(raw, sort_keys=True, default=str))}"


class TaskCommandLedger:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DEFAULT_LEDGER_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_channel TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    command_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    text_preview TEXT,
                    text_hash TEXT,
                    reason TEXT,
                    task_title TEXT,
                    task_page_id TEXT,
                    audit_id TEXT,
                    ack_status TEXT,
                    ack_message_id TEXT,
                    created_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    applied_at TEXT,
                    acknowledged_at TEXT,
                    metadata_json TEXT,
                    UNIQUE(source_ref, command_fingerprint)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_commands_recent ON task_commands(last_seen_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_commands_status ON task_commands(status)")
            conn.commit()

    def record_seen(self, command: dict[str, Any], *, status: str = "seen", reason: str | None = None) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid_task_command_status:{status}")
        now = utc_now_iso()
        source = str(command.get("source") or "Command")
        source_channel = str(command.get("source_channel") or source.lower())
        source_ref = str(command.get("source_ref") or f"command:{hash_text(json.dumps(command, sort_keys=True, default=str))}")
        fingerprint = str(command.get("command_fingerprint") or command_fingerprint(command))
        text = command.get("text") or command.get("summary") or ""
        metadata = {
            key: command.get(key)
            for key in ("channel_id", "message_id", "message_id_hash", "thread_id_hash", "meeting_title", "owner_hint")
            if command.get(key) is not None
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_commands (
                    source, source_channel, source_ref, command_fingerprint, status,
                    text_preview, text_hash, reason, created_at, first_seen_at, last_seen_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_ref, command_fingerprint) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN task_commands.status IN ('seen', 'blocked', 'manual_review_required') THEN excluded.status ELSE task_commands.status END,
                    reason=COALESCE(excluded.reason, task_commands.reason),
                    metadata_json=excluded.metadata_json
                """,
                (
                    source,
                    source_channel,
                    source_ref,
                    fingerprint,
                    status,
                    safe_preview(text),
                    hash_text(text),
                    reason,
                    str(command.get("created_at") or now),
                    now,
                    now,
                    json.dumps(metadata, sort_keys=True, default=str),
                ),
            )
            conn.commit()
        return self.get(source_ref=source_ref, command_fingerprint=fingerprint) or {}

    def update_outcome(self, *, source_ref: str, command_fingerprint: str, status: str, reason: str | None = None, task: dict[str, Any] | None = None, audit_id: str | None = None) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid_task_command_status:{status}")
        now = utc_now_iso()
        task = task or {}
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE task_commands
                SET status=?, reason=?, task_title=?, task_page_id=?, audit_id=COALESCE(?, audit_id), applied_at=?
                WHERE source_ref=? AND command_fingerprint=?
                """,
                (
                    status,
                    reason,
                    safe_preview(task.get("title") or "", limit=140) if task else None,
                    str(task.get("page_id") or task.get("id") or "") if task else None,
                    audit_id,
                    now if status in {"applied", "skipped_duplicate", "blocked", "manual_review_required"} else None,
                    source_ref,
                    command_fingerprint,
                ),
            )
            conn.commit()
        return self.get(source_ref=source_ref, command_fingerprint=command_fingerprint) or {}

    def update_ack(self, *, source_ref: str, command_fingerprint: str, status: str, message_id: str | None = None, reason: str | None = None) -> dict[str, Any]:
        if status not in {"ack_sent", "ack_failed"}:
            raise ValueError(f"invalid_ack_status:{status}")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE task_commands
                SET status=?, ack_status=?, ack_message_id=?, reason=COALESCE(?, reason), acknowledged_at=?
                WHERE source_ref=? AND command_fingerprint=?
                """,
                (status, status, message_id, reason, now, source_ref, command_fingerprint),
            )
            conn.commit()
        return self.get(source_ref=source_ref, command_fingerprint=command_fingerprint) or {}

    def get(self, *, source_ref: str, command_fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_commands WHERE source_ref=? AND command_fingerprint=?",
                (source_ref, command_fingerprint),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_commands ORDER BY last_seen_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def counts_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM task_commands GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def counts_by_source(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT source_channel, COUNT(*) AS count FROM task_commands GROUP BY source_channel").fetchall()
        return {str(row["source_channel"]): int(row["count"]) for row in rows}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = {key: row[key] for key in row.keys()}
    metadata = payload.pop("metadata_json", None)
    try:
        payload["metadata"] = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        payload["metadata"] = {}
    return payload
