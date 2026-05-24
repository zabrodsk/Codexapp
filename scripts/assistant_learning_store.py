#!/usr/bin/env python3
"""Sanitized outcome and preference store for Rocky assistant learning."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEARNING_DB_PATH = ROOT / "improvement" / "assistant_learning.sqlite3"
SCHEMA_VERSION = 1
SENSITIVE_KEY_PARTS = {
    "auth", "body", "content", "cookie", "credential", "description", "diff",
    "email_body", "html", "notes", "password", "raw", "secret", "token", "transcript",
}
SENSITIVE_TEXT_RE = re.compile(
    r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
MAX_SAFE_STRING_CHARS = 500


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(value: Any, *, prefix: str) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _redacted_value(value: Any) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return {"redacted": True, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], "chars": len(text)}


def sanitize_payload(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and _is_sensitive_key(parent_key):
        return _redacted_value(value)
    if isinstance(value, dict):
        return {str(key): sanitize_payload(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        return _redacted_value(value)
    if isinstance(value, str) and len(value) > MAX_SAFE_STRING_CHARS:
        return {"truncated": True, "prefix": value[:MAX_SAFE_STRING_CHARS], "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], "chars": len(value)}
    return value


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    mapping = {
        "evidence_json": ("evidence", {}),
        "metadata_json": ("metadata", {}),
        "source_refs_json": ("source_refs", []),
        "model_json": ("model", {}),
        "bounds_json": ("bounds", {}),
        "proposal_json": ("proposal", {}),
    }
    for raw_key, (safe_key, default) in mapping.items():
        if raw_key in payload:
            payload[safe_key] = json_loads(payload.pop(raw_key), default)
    return payload


class AssistantLearningStore:
    """Small SQLite store for sanitized outcome learning."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else DEFAULT_LEARNING_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assistant_outcomes (
                    observation_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    outcome_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    idempotency_key TEXT,
                    predicted_minutes REAL,
                    booked_minutes REAL,
                    actual_minutes REAL,
                    outcome_status TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    safe_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assistant_outcomes_lane ON assistant_outcomes(lane, outcome_type, observed_at);
                CREATE INDEX IF NOT EXISTS idx_assistant_outcomes_source ON assistant_outcomes(source_ref);
                CREATE TABLE IF NOT EXISTS assistant_preference_models (
                    preference_key TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value REAL,
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    model_json TEXT NOT NULL DEFAULT '{}',
                    bounds_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_learning_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    preference_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_assistant_learning_proposals_status ON assistant_learning_proposals(status, created_at);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        safe = sanitize_payload(dict(outcome))
        observation_id = str(safe.get("observation_id") or stable_id({
            "lane": safe.get("lane"),
            "outcome_type": safe.get("outcome_type"),
            "source_ref": safe.get("source_ref"),
            "idempotency_key": safe.get("idempotency_key"),
            "outcome_status": safe.get("outcome_status"),
        }, prefix="outcome"))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_outcomes (
                    observation_id, observed_at, lane, outcome_type, source_ref, idempotency_key,
                    predicted_minutes, booked_minutes, actual_minutes, outcome_status, confidence,
                    evidence_json, source_refs_json, safe_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    lane=excluded.lane,
                    outcome_type=excluded.outcome_type,
                    source_ref=excluded.source_ref,
                    idempotency_key=excluded.idempotency_key,
                    predicted_minutes=excluded.predicted_minutes,
                    booked_minutes=excluded.booked_minutes,
                    actual_minutes=excluded.actual_minutes,
                    outcome_status=excluded.outcome_status,
                    confidence=excluded.confidence,
                    evidence_json=excluded.evidence_json,
                    source_refs_json=excluded.source_refs_json,
                    safe_summary=excluded.safe_summary,
                    updated_at=excluded.updated_at
                """,
                (
                    observation_id,
                    str(safe.get("observed_at") or now),
                    str(safe.get("lane") or "unknown"),
                    str(safe.get("outcome_type") or "unknown"),
                    str(safe.get("source_ref") or observation_id),
                    safe.get("idempotency_key"),
                    _float_or_none(safe.get("predicted_minutes")),
                    _float_or_none(safe.get("booked_minutes")),
                    _float_or_none(safe.get("actual_minutes")),
                    str(safe.get("outcome_status") or "observed"),
                    float(safe.get("confidence") or 0),
                    json_dumps(safe.get("evidence") or {}),
                    json_dumps(safe.get("source_refs") or []),
                    str(safe.get("safe_summary") or "Outcome observed")[:500],
                    now,
                    now,
                ),
            )
        return self.get_outcome(observation_id) or {"observation_id": observation_id}

    def get_outcome(self, observation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM assistant_outcomes WHERE observation_id = ?", (observation_id,)).fetchone()
        return row_to_dict(row)

    def list_outcomes(self, *, lane: str | None = None, outcome_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if lane:
            clauses.append("lane = ?")
            params.append(lane)
        if outcome_type:
            clauses.append("outcome_type = ?")
            params.append(outcome_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM assistant_outcomes {where} ORDER BY observed_at DESC, updated_at DESC LIMIT ?", params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def upsert_preference_model(self, model: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        safe = sanitize_payload(dict(model))
        key = str(safe.get("preference_key") or "")
        if not key:
            raise ValueError("preference_key is required")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_preference_models (
                    preference_key, lane, status, value, confidence, evidence_count,
                    model_json, bounds_json, reason, updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    lane=excluded.lane,
                    status=excluded.status,
                    value=excluded.value,
                    confidence=excluded.confidence,
                    evidence_count=excluded.evidence_count,
                    model_json=excluded.model_json,
                    bounds_json=excluded.bounds_json,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    str(safe.get("lane") or "assistant"),
                    str(safe.get("status") or "insufficient_evidence"),
                    _float_or_none(safe.get("value")),
                    float(safe.get("confidence") or 0),
                    int(safe.get("evidence_count") or 0),
                    json_dumps(safe.get("model") or {}),
                    json_dumps(safe.get("bounds") or {}),
                    str(safe.get("reason") or "preference_model_updated")[:500],
                    now,
                    now,
                ),
            )
        return self.get_preference_model(key) or {"preference_key": key}

    def get_preference_model(self, preference_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM assistant_preference_models WHERE preference_key = ?", (preference_key,)).fetchone()
        return row_to_dict(row)

    def list_preference_models(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM assistant_preference_models ORDER BY updated_at DESC").fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_learning_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        safe = sanitize_payload(dict(proposal))
        proposal_id = str(safe.get("proposal_id") or stable_id({"preference_key": safe.get("preference_key"), "proposal_type": safe.get("proposal_type"), "reason": safe.get("reason")}, prefix="learning-proposal"))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_learning_proposals (
                    proposal_id, preference_key, status, proposal_type, reason, confidence,
                    evidence_count, proposal_json, created_at, updated_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    confidence=excluded.confidence,
                    evidence_count=excluded.evidence_count,
                    proposal_json=excluded.proposal_json,
                    updated_at=excluded.updated_at
                """,
                (proposal_id, str(safe.get("preference_key") or "unknown"), str(safe.get("status") or "proposed"), str(safe.get("proposal_type") or "review_required"), str(safe.get("reason") or "learning_proposal_created")[:500], float(safe.get("confidence") or 0), int(safe.get("evidence_count") or 0), json_dumps(safe.get("proposal") or {}), now, now),
            )
        return self.get_learning_proposal(proposal_id) or {"proposal_id": proposal_id}

    def get_learning_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM assistant_learning_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
        return row_to_dict(row)

    def list_learning_proposals(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM assistant_learning_proposals {where} ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def mark_proposal_applied(self, proposal_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("UPDATE assistant_learning_proposals SET status='applied', applied_at=?, updated_at=? WHERE proposal_id=?", (now, now, proposal_id))
        return self.get_learning_proposal(proposal_id)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def read_active_bounded_preference(preference_key: str, *, db_path: str | Path | None = None) -> dict[str, Any] | None:
    path = Path(db_path) if db_path else DEFAULT_LEARNING_DB_PATH
    if not path.exists():
        return None
    try:
        store = AssistantLearningStore(path)
        row = store.get_preference_model(preference_key)
    except Exception:
        return None
    if not row or row.get("status") != "active_bounded":
        return None
    return row
