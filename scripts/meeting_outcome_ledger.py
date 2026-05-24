#!/usr/bin/env python3
"""SQLite ledger for meeting outcome capture and follow-up application."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_DB_PATH = ROOT / "improvement" / "meeting_outcome_ledger.sqlite3"
SCHEMA_VERSION = 1
SENSITIVE_RE = re.compile(r"(token|secret|password|credential|cookie|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)


class MeetingOutcomeLedger:
    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else DEFAULT_LEDGER_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meeting_outcomes (
                    ledger_id TEXT PRIMARY KEY,
                    meeting_key TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    outcome_hash TEXT,
                    status TEXT NOT NULL,
                    title_preview TEXT,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    task_refs_json TEXT NOT NULL DEFAULT '[]',
                    memory_refs_json TEXT NOT NULL DEFAULT '[]',
                    audit_ids_json TEXT NOT NULL DEFAULT '[]',
                    reason TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_outcomes_identity
                    ON meeting_outcomes(meeting_key, source_ref, COALESCE(outcome_hash, ''));

                CREATE INDEX IF NOT EXISTS idx_meeting_outcomes_status
                    ON meeting_outcomes(status, last_updated_at);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record_seen(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self.upsert(
            meeting_key=str(candidate.get("meeting_key") or ""),
            source_ref=str(candidate.get("source_ref") or ""),
            outcome_hash=None,
            status="seen",
            title_preview=str(candidate.get("title") or ""),
            summary={"meeting_date": candidate.get("meeting_date"), "structured_line_count": candidate.get("structured_line_count")},
            reason="meeting_outcome_candidate_seen",
        )

    def record_outcome(self, outcome: dict[str, Any], *, status: str, reason: str | None = None, task_refs: list[Any] | None = None, memory_refs: list[Any] | None = None, audit_ids: list[Any] | None = None) -> dict[str, Any]:
        return self.upsert(
            meeting_key=str(outcome.get("meeting_key") or ""),
            source_ref=str(outcome.get("source_ref") or ""),
            outcome_hash=str(outcome.get("outcome_hash") or ""),
            status=status,
            title_preview=str(outcome.get("title") or ""),
            summary={
                "decision_count": outcome.get("decision_count"),
                "follow_up_count": outcome.get("follow_up_count"),
                "relationship_update_count": outcome.get("relationship_update_count"),
                "fallback_used": outcome.get("fallback_used"),
            },
            task_refs=task_refs or [],
            memory_refs=memory_refs or [],
            audit_ids=audit_ids or [],
            reason=reason or outcome.get("reason"),
        )

    def upsert(
        self,
        *,
        meeting_key: str,
        source_ref: str,
        outcome_hash: str | None,
        status: str,
        title_preview: str,
        summary: dict[str, Any] | None = None,
        task_refs: list[Any] | None = None,
        memory_refs: list[Any] | None = None,
        audit_ids: list[Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        ledger_id = _stable_id({"meeting_key": meeting_key, "source_ref": source_ref, "outcome_hash": outcome_hash or ""})
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                INSERT INTO meeting_outcomes (
                    ledger_id, meeting_key, source_ref, outcome_hash, status,
                    title_preview, summary_json, task_refs_json, memory_refs_json,
                    audit_ids_json, reason, first_seen_at, last_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ledger_id) DO UPDATE SET
                    status = excluded.status,
                    title_preview = excluded.title_preview,
                    summary_json = excluded.summary_json,
                    task_refs_json = excluded.task_refs_json,
                    memory_refs_json = excluded.memory_refs_json,
                    audit_ids_json = excluded.audit_ids_json,
                    reason = excluded.reason,
                    last_updated_at = excluded.last_updated_at
                """,
                (
                    ledger_id,
                    meeting_key,
                    source_ref,
                    outcome_hash or "",
                    status,
                    _safe_text(title_preview, 220),
                    _json(summary or {}),
                    _json(task_refs or []),
                    _json(memory_refs or []),
                    _json(audit_ids or []),
                    _safe_text(reason, 220),
                    now,
                    now,
                ),
            )
        return self.get(ledger_id) or {"ledger_id": ledger_id}

    def get(self, ledger_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM meeting_outcomes WHERE ledger_id = ?", (ledger_id,)).fetchone()
        return _row_to_dict(row)

    def get_for_outcome(self, outcome: dict[str, Any]) -> dict[str, Any] | None:
        ledger_id = _stable_id(
            {
                "meeting_key": str(outcome.get("meeting_key") or ""),
                "source_ref": str(outcome.get("source_ref") or ""),
                "outcome_hash": str(outcome.get("outcome_hash") or ""),
            }
        )
        return self.get(ledger_id)

    def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM meeting_outcomes ORDER BY last_updated_at DESC LIMIT ?", (max(0, int(limit)),)).fetchall()
        return [_row_to_dict(row) for row in rows if row is not None]

    def counts_by_status(self) -> dict[str, int]:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM meeting_outcomes GROUP BY status").fetchall()
        return {str(status): int(count) for status, count in rows}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    for key in ["summary_json", "task_refs_json", "memory_refs_json", "audit_ids_json"]:
        out_key = key.replace("_json", "")
        try:
            payload[out_key] = json.loads(payload.get(key) or "[]" if key.endswith("refs_json") or key == "audit_ids_json" else payload.get(key) or "{}")
        except Exception:
            payload[out_key] = [] if key.endswith("refs_json") or key == "audit_ids_json" else {}
        payload.pop(key, None)
    return payload


def _json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, default=str)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value, 1000)
    return value


def _safe_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _stable_id(value: Any) -> str:
    return "meeting-outcome:" + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
