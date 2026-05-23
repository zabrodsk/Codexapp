#!/usr/bin/env python3
"""SQLite state store for Rocky assistant scheduler health.

This database is deliberately small and local. It records scheduler health,
locks, and dead-letter items for assistant workflows without becoming a
general-purpose queue.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEDULER_DB_PATH = ROOT / "improvement" / "assistant_scheduler.sqlite3"
SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(value: Any, *, prefix: str) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class AssistantSchedulerState:
    """Small SQLite state store for scheduler reliability primitives."""

    def __init__(self, db_path: Path | str | None = None):
        self.path = Path(db_path) if db_path else DEFAULT_SCHEDULER_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with sqlite3.connect(str(self.path), timeout=5) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assistant_job_runs (
                    run_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    job_label TEXT,
                    scheduled_for TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    idempotency_key TEXT NOT NULL,
                    launchagent_label TEXT,
                    program TEXT,
                    exit_code INTEGER,
                    failure_class TEXT,
                    summary TEXT,
                    error_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_assistant_job_runs_job
                    ON assistant_job_runs(job_name, created_at);

                CREATE INDEX IF NOT EXISTS idx_assistant_job_runs_idempotency
                    ON assistant_job_runs(idempotency_key);

                CREATE TABLE IF NOT EXISTS assistant_run_locks (
                    lock_key TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    owner TEXT,
                    pid INTEGER,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_assistant_run_locks_workflow
                    ON assistant_run_locks(workflow, status);

                CREATE TABLE IF NOT EXISTS assistant_dead_letters (
                    dead_letter_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    workflow TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    failure_class TEXT NOT NULL,
                    error_hash TEXT,
                    safe_summary TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    recovery_hint TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_dead_letters_open_key
                    ON assistant_dead_letters(job_name, idempotency_key, failure_class, status);
                """
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record_job_run(
        self,
        *,
        job_name: str,
        status: str,
        idempotency_key: str,
        job_label: str | None = None,
        scheduled_for: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        attempt: int = 1,
        launchagent_label: str | None = None,
        program: str | None = None,
        exit_code: int | None = None,
        failure_class: str | None = None,
        summary: str | None = None,
        error_hash: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        run_id = stable_id(
            {
                "job_name": job_name,
                "idempotency_key": idempotency_key,
                "status": status,
                "created_at": now,
                "nonce": str(uuid.uuid4()),
            },
            prefix="run",
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assistant_job_runs (
                    run_id, job_name, job_label, scheduled_for, started_at, finished_at,
                    status, attempt, idempotency_key, launchagent_label, program,
                    exit_code, failure_class, summary, error_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_name,
                    job_label,
                    scheduled_for,
                    started_at,
                    finished_at,
                    status,
                    attempt,
                    idempotency_key,
                    launchagent_label,
                    program,
                    exit_code,
                    failure_class,
                    summary,
                    error_hash,
                    now,
                    now,
                ),
            )
        return self.get_job_run(run_id) or {"run_id": run_id}

    def get_job_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_job_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row_to_dict(row)

    def list_job_runs(self, *, job_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM assistant_job_runs"
        params: list[Any] = []
        if job_name:
            sql += " WHERE job_name = ?"
            params.append(job_name)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_dead_letter(
        self,
        *,
        job_name: str,
        workflow: str,
        idempotency_key: str,
        failure_class: str,
        safe_summary: str,
        source_refs: list[Any] | None = None,
        recovery_hint: str | None = None,
        error_hash: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        dead_letter_id = stable_id(
            {
                "job_name": job_name,
                "idempotency_key": idempotency_key,
                "failure_class": failure_class,
            },
            prefix="dead-letter",
        )
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM assistant_dead_letters
                WHERE dead_letter_id = ? AND status = 'open'
                """,
                (dead_letter_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE assistant_dead_letters
                    SET attempts = attempts + 1,
                        last_failed_at = ?,
                        safe_summary = ?,
                        source_refs_json = ?,
                        recovery_hint = ?,
                        error_hash = ?,
                        updated_at = ?
                    WHERE dead_letter_id = ?
                    """,
                    (
                        now,
                        safe_summary,
                        json_dumps(source_refs or []),
                        recovery_hint,
                        error_hash,
                        now,
                        dead_letter_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO assistant_dead_letters (
                        dead_letter_id, job_name, workflow, idempotency_key,
                        first_failed_at, last_failed_at, attempts, failure_class,
                        error_hash, safe_summary, source_refs_json, recovery_hint,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        dead_letter_id,
                        job_name,
                        workflow,
                        idempotency_key,
                        now,
                        now,
                        1,
                        failure_class,
                        error_hash,
                        safe_summary,
                        json_dumps(source_refs or []),
                        recovery_hint,
                        now,
                        now,
                    ),
                )
        return self.get_dead_letter(dead_letter_id) or {"dead_letter_id": dead_letter_id}

    def get_dead_letter(self, dead_letter_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_dead_letters WHERE dead_letter_id = ?",
                (dead_letter_id,),
            ).fetchone()
        return row_to_dict(row)

    def list_dead_letters(self, *, status: str | None = "open", limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM assistant_dead_letters"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def update_dead_letter_status(self, dead_letter_id: str, status: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE assistant_dead_letters
                SET status = ?, updated_at = ?
                WHERE dead_letter_id = ?
                """,
                (status, now, dead_letter_id),
            )
        return self.get_dead_letter(dead_letter_id)

    def acquire_lock_record(
        self,
        *,
        lock_key: str,
        workflow: str,
        idempotency_key: str,
        owner: str,
        pid: int,
        acquired_at: str,
        expires_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Acquire a lock atomically.

        Returns `(result, row)` where result is `acquired`,
        `duplicate_blocked`, or `stale_recovered`.
        """
        for attempt in range(3):
            try:
                return self._acquire_lock_record_once(
                    lock_key=lock_key,
                    workflow=workflow,
                    idempotency_key=idempotency_key,
                    owner=owner,
                    pid=pid,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                    metadata=metadata or {},
                )
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
        raise RuntimeError("unreachable lock retry state")

    def _acquire_lock_record_once(
        self,
        *,
        lock_key: str,
        workflow: str,
        idempotency_key: str,
        owner: str,
        pid: int,
        acquired_at: str,
        expires_at: str,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM assistant_run_locks WHERE lock_key = ?",
                (lock_key,),
            ).fetchone()
            now = acquired_at
            if row and row["status"] == "active" and str(row["expires_at"]) > now:
                conn.commit()
                return "duplicate_blocked", dict(row)
            result = "stale_recovered" if row and row["status"] == "active" else "acquired"
            conn.execute(
                """
                INSERT INTO assistant_run_locks (
                    lock_key, workflow, idempotency_key, owner, pid, acquired_at,
                    expires_at, released_at, status, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    workflow = excluded.workflow,
                    idempotency_key = excluded.idempotency_key,
                    owner = excluded.owner,
                    pid = excluded.pid,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    released_at = NULL,
                    status = 'active',
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    lock_key,
                    workflow,
                    idempotency_key,
                    owner,
                    pid,
                    acquired_at,
                    expires_at,
                    json_dumps(metadata),
                    acquired_at,
                ),
            )
            new_row = conn.execute(
                "SELECT * FROM assistant_run_locks WHERE lock_key = ?",
                (lock_key,),
            ).fetchone()
            conn.commit()
            return result, dict(new_row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_lock_record(self, *, lock_key: str, released_at: str | None = None) -> dict[str, Any] | None:
        released_at = released_at or utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE assistant_run_locks
                SET status = 'released',
                    released_at = ?,
                    updated_at = ?
                WHERE lock_key = ? AND status = 'active'
                """,
                (released_at, released_at, lock_key),
            )
            row = conn.execute(
                "SELECT * FROM assistant_run_locks WHERE lock_key = ?",
                (lock_key,),
            ).fetchone()
        return row_to_dict(row)

    def get_lock(self, lock_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_run_locks WHERE lock_key = ?",
                (lock_key,),
            ).fetchone()
        return row_to_dict(row)
