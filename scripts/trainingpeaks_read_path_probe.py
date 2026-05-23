#!/usr/bin/env python3
"""Read-only TrainingPeaks read-path discovery for Rocky Sprint 4.0."""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apple_calendar_cli import DEFAULT_DB_PATH, query_events
from trainingpeaks_ics_reader import TIMEZONE


DEFAULT_WEBCAL_URL_FILE = Path.home() / ".openclaw" / "secrets" / "trainingpeaks-webcal-url"
DEFAULT_EVENTS_DB = Path.home() / ".openclaw" / "events.db"


def probe_trainingpeaks_read_paths(
    *,
    calendar_db_path: str | Path | None = None,
    events_db_path: str | Path | None = None,
    webcal_url_file: str | Path | None = None,
    start_date: str | None = None,
    days_ahead: int = 14,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    start = _parse_start(start_date)
    calendar_probe = _probe_apple_calendar_feed(
        db_path=Path(calendar_db_path).expanduser() if calendar_db_path else DEFAULT_DB_PATH,
        start=start,
        days_ahead=days_ahead,
        calendar_events=calendar_events,
    )
    webcal_probe = _probe_webcal_url_file(Path(webcal_url_file).expanduser() if webcal_url_file else _env_or_default_webcal_file())
    official_api_probe = _probe_official_api_config()
    community_mcp_probe = _probe_community_mcp_static()
    whoop_probe = _probe_whoop_events(Path(events_db_path).expanduser() if events_db_path else DEFAULT_EVENTS_DB)
    paths = {
        "apple_calendar_subscribed_feed": calendar_probe,
        "direct_ics_webcal_url_file": webcal_probe,
        "official_trainingpeaks_api": official_api_probe,
        "community_trainingpeaks_mcp": community_mcp_probe,
        "whoop_timing_evidence": whoop_probe,
    }
    recommendation = _recommend(paths)
    return {
        "status": "ok" if recommendation["recommended_path"] else "blocked",
        "observed_at": datetime.now(tz=ZoneInfo(TIMEZONE)).isoformat(),
        "window": {
            "start_date": start.date().isoformat(),
            "days_ahead": int(days_ahead),
            "timezone": TIMEZONE,
        },
        "recommendation": recommendation,
        "paths": paths,
        "security_boundaries": [
            "no_password_prompt",
            "no_cookie_extraction",
            "no_browser_automation",
            "no_mcp_installation",
            "no_trainingpeaks_write",
            "no_calendar_write",
        ],
        "calendar_write_attempted": False,
    }


def _probe_apple_calendar_feed(
    *,
    db_path: Path,
    start: datetime,
    days_ahead: int,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if calendar_events is None:
        if not db_path.exists():
            return {"status": "missing", "reason": "calendar_db_missing", "safe_path": str(db_path)}
        try:
            calendar_events = query_events(
                db_path=db_path,
                start=start.replace(tzinfo=None),
                end=(start + timedelta(days=days_ahead)).replace(tzinfo=None),
                include_all_day=True,
            )
        except sqlite3.Error as exc:
            return {
                "status": "blocked",
                "reason": "calendar_db_query_failed",
                "safe_error": type(exc).__name__,
            }
    candidates = [_sanitize_calendar_candidate(event) for event in calendar_events if _looks_like_trainingpeaks_event(event)]
    return {
        "status": "available" if candidates else "not_found",
        "candidate_count": len(candidates),
        "safe_candidates": candidates[:10],
        "notes": [
            "Apple Calendar subscribed feeds are read-only for Rocky Sprint 4.0.",
            "Candidate detection uses calendar/title metadata only and does not return raw notes.",
        ],
    }


def _probe_webcal_url_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "not_configured",
            "safe_path": str(path),
            "url_redacted": True,
        }
    warnings: list[str] = []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        warnings.append("webcal_url_file_permissions_too_open")
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if parent_mode & 0o077:
        warnings.append("webcal_url_parent_directory_permissions_too_open")
    has_content = bool(path.read_text(encoding="utf-8", errors="replace").strip())
    return {
        "status": "available" if has_content else "blocked",
        "reason": None if has_content else "webcal_url_file_empty",
        "safe_path": str(path),
        "url_redacted": True,
        "permission_warnings": warnings,
        "notes": ["Use trainingpeaks-ics-preview --webcal-url-file to fetch and parse this source."],
    }


def _probe_official_api_config() -> dict[str, Any]:
    configured = bool(os.environ.get("TRAININGPEAKS_API_CLIENT_ID") and os.environ.get("TRAININGPEAKS_API_CLIENT_SECRET"))
    return {
        "status": "configured_but_not_used" if configured else "not_configured",
        "reason": None if configured else "approved_personal_api_access_not_detected",
        "notes": [
            "TrainingPeaks says API access is for approved developers and not available for personal use.",
            "Sprint 4.0 does not call the API.",
        ],
    }


def _probe_community_mcp_static() -> dict[str, Any]:
    tp_mcp = shutil.which("tp-mcp")
    return {
        "status": "installed_but_not_trusted" if tp_mcp else "not_installed",
        "safe_executable_path": tp_mcp,
        "reason": "write_tools_exposed_by_default",
        "notes": [
            "Sprint 4.0 does not install, authenticate, or run community TrainingPeaks MCP tools.",
            "Audit is required before any read-only wrapper can be trusted.",
        ],
    }


def _probe_whoop_events(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "safe_path": str(path)}
    try:
        with sqlite3.connect(str(path)) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       MIN(datetime(start_ts_utc, 'unixepoch')) AS first_utc,
                       MAX(datetime(start_ts_utc, 'unixepoch')) AS last_utc
                  FROM events
                 WHERE source = 'whoop'
                """
            ).fetchone()
    except sqlite3.Error as exc:
        return {"status": "blocked", "reason": "events_db_query_failed", "safe_error": type(exc).__name__}
    count = int(row[0] or 0)
    return {
        "status": "available" if count else "empty",
        "safe_path": str(path),
        "event_count": count,
        "first_event_utc": row[1],
        "last_event_utc": row[2],
        "use": "timing_sanity_check_only",
    }


def _looks_like_trainingpeaks_event(event: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(event.get(key) or "")
        for key in ("calendar", "summary", "location")
    ).lower()
    return "trainingpeaks" in haystack or "training peaks" in haystack


def _sanitize_calendar_candidate(event: dict[str, Any]) -> dict[str, Any]:
    summary = " ".join(str(event.get("summary") or "").split())
    return {
        "summary_hash": _hash(summary),
        "summary_preview": summary[:80],
        "start_local": event.get("start_local"),
        "end_local": event.get("end_local"),
        "all_day": bool(event.get("all_day")),
        "calendar": event.get("calendar"),
    }


def _recommend(paths: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if paths["apple_calendar_subscribed_feed"].get("status") == "available":
        return {
            "recommended_path": "apple_calendar_subscribed_feed",
            "decision": "go_for_sprint_4_1_read_only_planner",
            "reason": "TrainingPeaks feed appears in Apple Calendar and can be read without new secrets.",
        }
    webcal = paths["direct_ics_webcal_url_file"]
    if webcal.get("status") == "available" and not webcal.get("permission_warnings"):
        return {
            "recommended_path": "direct_ics_webcal_url_file",
            "decision": "go_for_sprint_4_1_after_preview_smoke",
            "reason": "A secret webcal URL file exists with acceptable permissions.",
        }
    if webcal.get("status") == "available":
        return {
            "recommended_path": None,
            "decision": "blocked_until_secret_permissions_are_fixed",
            "reason": "The webcal URL file exists but permissions are too open.",
        }
    return {
        "recommended_path": None,
        "decision": "blocked_missing_trainingpeaks_source",
        "reason": "No Apple Calendar TrainingPeaks feed or configured webcal URL file was detected.",
    }


def _parse_start(value: str | None) -> datetime:
    if value:
        return datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=ZoneInfo(TIMEZONE))
    today = datetime.now(tz=ZoneInfo(TIMEZONE)).date()
    return datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(TIMEZONE))


def _env_or_default_webcal_file() -> Path:
    env_path = os.environ.get("TRAININGPEAKS_WEBCAL_URL_FILE")
    return Path(env_path).expanduser() if env_path else DEFAULT_WEBCAL_URL_FILE


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


__all__ = [
    "DEFAULT_EVENTS_DB",
    "DEFAULT_WEBCAL_URL_FILE",
    "probe_trainingpeaks_read_paths",
]
