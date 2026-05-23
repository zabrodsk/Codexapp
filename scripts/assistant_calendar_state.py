#!/usr/bin/env python3
"""SQLite state store for Rocky-owned Apple Calendar blocks."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALENDAR_STATE_DB_PATH = ROOT / "improvement" / "assistant_calendar.sqlite3"
SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class AssistantCalendarState:
    """Small SQLite ledger for live Rocky calendar block lifecycle state."""

    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path) if db_path else DEFAULT_CALENDAR_STATE_DB_PATH
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
                CREATE TABLE IF NOT EXISTS assistant_calendar_blocks (
                    idempotency_key TEXT PRIMARY KEY,
                    calendar_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    event_uid TEXT,
                    status TEXT NOT NULL,
                    create_audit_id TEXT,
                    delete_audit_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_assistant_calendar_blocks_status
                    ON assistant_calendar_blocks(status, calendar_name);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_calendar_blocks WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row_to_dict(row)

    def get_active(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM assistant_calendar_blocks
                WHERE idempotency_key = ? AND status = 'active'
                """,
                (idempotency_key,),
            ).fetchone()
        return row_to_dict(row)

    def list_blocks(
        self,
        *,
        calendar_name: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if calendar_name:
            clauses.append("calendar_name = ?")
            params.append(calendar_name)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM assistant_calendar_blocks
                {where}
                ORDER BY updated_at DESC, created_at DESC
                {limit_sql}
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_created(
        self,
        *,
        idempotency_key: str,
        calendar_name: str,
        title: str,
        start: str,
        end: str,
        event_uid: str | None,
        create_audit_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_calendar_blocks (
                    idempotency_key, calendar_name, title, start, end, event_uid,
                    status, create_audit_id, delete_audit_id, created_at,
                    updated_at, deleted_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, NULL, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    calendar_name = excluded.calendar_name,
                    title = excluded.title,
                    start = excluded.start,
                    end = excluded.end,
                    event_uid = excluded.event_uid,
                    status = 'active',
                    create_audit_id = excluded.create_audit_id,
                    delete_audit_id = NULL,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL,
                    metadata_json = excluded.metadata_json
                """,
                (
                    idempotency_key,
                    calendar_name,
                    title,
                    start,
                    end,
                    event_uid,
                    create_audit_id,
                    now,
                    now,
                    json_dumps(metadata or {}),
                ),
            )
        return self.get(idempotency_key) or {"idempotency_key": idempotency_key}

    def mark_deleted(self, *, idempotency_key: str, delete_audit_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE assistant_calendar_blocks
                SET status = 'deleted',
                    delete_audit_id = ?,
                    deleted_at = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (delete_audit_id, now, now, idempotency_key),
            )
        return self.get(idempotency_key)

    def mark_stale(self, *, idempotency_key: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE assistant_calendar_blocks
                SET status = 'stale',
                    updated_at = ?
                WHERE idempotency_key = ? AND status = 'active'
                """,
                (now, idempotency_key),
            )
        return self.get(idempotency_key)
