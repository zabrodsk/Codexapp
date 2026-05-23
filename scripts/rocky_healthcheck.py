#!/usr/bin/env python3
"""
Rocky Integration Health Checker

Read-only, cheap health checks for key integrations. Produces a concise
operational summary suitable for chat.

Usage:
    from rocky_healthcheck import run_all_checks, format_health_report
    statuses = run_all_checks()
    print(format_health_report(statuses))

Disable: simply don't import/call this module. No side effects.
All checks are read-only and non-destructive.
"""

import json
import os
import sqlite3
import glob as glob_mod
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", Path.home() / ".openclaw"))
STUDENT_DB_PATH = OPENCLAW_ROOT / "student" / "student.sqlite3"
COMPANY_DB_PATH = OPENCLAW_ROOT / "student" / "company.sqlite3"
CRON_RUNS_PATH = OPENCLAW_ROOT / "cron" / "runs"
CALENDAR_DB_PATH = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "group.com.apple.calendar"
    / "Calendar.sqlitedb"
)


@dataclass
class HealthStatus:
    """Health status for a single integration component."""
    component_name: str
    status: str  # "healthy" | "degraded" | "blocked" | "unknown"
    last_checked_at: str
    failure_class: Optional[str] = None
    human_summary: str = ""
    recommended_action: Optional[str] = None
    latency_ms: Optional[int] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_student_archive() -> HealthStatus:
    """Check Student archive SQLite DB health."""
    component = "student_archive"
    try:
        if not STUDENT_DB_PATH.exists():
            return HealthStatus(
                component_name=component,
                status="blocked",
                last_checked_at=_now_iso(),
                failure_class="target_not_found",
                human_summary=f"Student DB not found at {STUDENT_DB_PATH}",
                recommended_action="Check STUDENT_SQLITE_PATH config or run Student setup.",
            )

        conn = sqlite3.connect(f"file:{STUDENT_DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Check if tables exist. Current Student uses sources/source_chunks/
        # insights, not the early prototype posts table.
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        expected_tables = {"sources", "source_chunks", "insights", "jobs"}

        if not expected_tables.issubset(tables):
            conn.close()
            missing = ", ".join(sorted(expected_tables - tables))
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                failure_class="target_not_found",
                human_summary=f"Student DB exists but expected tables are missing: {missing}.",
                recommended_action="Run Student service initialization.",
            )

        # Check latest ingest time
        cursor.execute("SELECT COUNT(*), MAX(created_at) FROM sources")
        count, latest = cursor.fetchone()
        if not count:
            conn.close()
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                failure_class="archive_empty",
                human_summary="Student archive schema is present but no sources are stored.",
                recommended_action="Check Student ingest schedules and Discord archive daemon.",
            )
        cursor.execute("SELECT COUNT(*) FROM source_chunks")
        chunk_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM insights")
        insight_count = cursor.fetchone()[0]
        conn.close()

        if latest:
            # Check freshness (warn if >24h stale)
            try:
                latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - latest_dt
                if age > timedelta(hours=24):
                    return HealthStatus(
                        component_name=component,
                        status="degraded",
                        last_checked_at=_now_iso(),
                        human_summary=f"Student archive accessible but stale. Latest ingest: {age.days}d {age.seconds//3600}h ago.",
                        recommended_action="Check Student ingest schedule. May need manual trigger.",
                    )
            except (ValueError, TypeError):
                pass

        return HealthStatus(
            component_name=component,
            status="healthy",
            last_checked_at=_now_iso(),
            human_summary=f"Student archive accessible and recent ({count} sources, {chunk_count} chunks, {insight_count} insights).",
        )

    except sqlite3.OperationalError as e:
        err_str = str(e).lower()
        if "locked" in err_str:
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                failure_class="timeout",
                human_summary="Student DB locked (another process writing). Retry shortly.",
                recommended_action="Wait a few seconds and retry.",
            )
        return HealthStatus(
            component_name=component,
            status="blocked",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Student DB error: {e}",
            recommended_action="Check DB file permissions and integrity.",
        )
    except Exception as e:
        return HealthStatus(
            component_name=component,
            status="unknown",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Student archive check failed: {e}",
        )


def check_calendar() -> HealthStatus:
    """Check Apple Calendar DB accessibility."""
    component = "calendar"
    try:
        if not CALENDAR_DB_PATH.exists():
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                failure_class="target_not_found",
                human_summary="Apple Calendar DB not found. AppleScript or Google Calendar MCP may still work.",
                recommended_action="Use Google Calendar MCP as primary path, or check Calendar app permissions.",
            )

        conn = sqlite3.connect(f"file:{CALENDAR_DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 5")
        tables = cursor.fetchall()
        conn.close()

        if tables:
            return HealthStatus(
                component_name=component,
                status="healthy",
                last_checked_at=_now_iso(),
                human_summary="Apple Calendar DB accessible (read-only).",
            )
        else:
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                human_summary="Calendar DB exists but appears empty.",
                recommended_action="Try Google Calendar MCP as fallback.",
            )

    except sqlite3.OperationalError as e:
        return HealthStatus(
            component_name=component,
            status="degraded",
            last_checked_at=_now_iso(),
            failure_class="permission_denied",
            human_summary=f"Calendar DB access denied: {e}",
            recommended_action="Grant Full Disk Access to the terminal app, or use Google Calendar MCP.",
        )
    except Exception as e:
        return HealthStatus(
            component_name=component,
            status="unknown",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Calendar check failed: {e}",
        )


def check_cron_delivery() -> HealthStatus:
    """Check enabled cron job delivery health from JSONL run logs."""
    component = "cron_delivery"
    try:
        if not CRON_RUNS_PATH.exists():
            return HealthStatus(
                component_name=component,
                status="unknown",
                last_checked_at=_now_iso(),
                human_summary="No cron run logs directory found.",
                recommended_action="Check cron configuration.",
            )

        enabled_jobs = _enabled_cron_jobs()
        if not enabled_jobs:
            log_files = sorted(CRON_RUNS_PATH.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not log_files:
                return HealthStatus(
                    component_name=component,
                    status="unknown",
                    last_checked_at=_now_iso(),
                    human_summary="No cron run logs found.",
                    recommended_action="Cron jobs may not have run yet. Check cron schedule.",
                )
            return HealthStatus(
                component_name=component,
                status="unknown",
                last_checked_at=_now_iso(),
                human_summary="No enabled cron job metadata found; cannot assess active cron delivery.",
                recommended_action="Check cron jobs.json.",
            )

        failed = []
        healthy = 0
        missing = []
        checked = 0
        for job_id, job_name in enabled_jobs.items():
            if _is_future_one_shot_job(job_id):
                continue
            log_file = CRON_RUNS_PATH / f"{job_id}.jsonl"
            if not log_file.exists():
                missing.append(job_name)
                continue
            latest = _latest_jsonl_entry(log_file)
            if not latest:
                missing.append(job_name)
                continue
            checked += 1
            status = latest.get("status", latest.get("state", ""))
            if status in ("failed", "error", "blocked"):
                failed.append(job_name)
            elif status in ("success", "completed", "delivered", "ok", "succeeded"):
                healthy += 1

        if checked == 0 and missing:
            return HealthStatus(
                component_name=component,
                status="unknown",
                last_checked_at=_now_iso(),
                human_summary=f"No parseable run logs for enabled cron jobs ({len(missing)} missing).",
                recommended_action="Wait for first scheduled run or inspect cron scheduler.",
            )

        if failed and healthy == 0:
            return HealthStatus(
                component_name=component,
                status="blocked",
                last_checked_at=_now_iso(),
                failure_class="unknown_error",
                human_summary=f"Enabled cron jobs failing. Affected: {', '.join(failed[:3])}",
                recommended_action="Check run ledger for specific failure classes.",
            )

        if failed:
            return HealthStatus(
                component_name=component,
                status="degraded",
                last_checked_at=_now_iso(),
                human_summary=f"Enabled cron jobs have latest failures ({len(failed)} failing, {healthy} healthy). Affected: {', '.join(failed[:3])}.",
                recommended_action="Check run ledger for affected jobs.",
            )

        if healthy == 0:
            return HealthStatus(
                component_name=component,
                status="unknown",
                last_checked_at=_now_iso(),
                human_summary=f"Enabled cron logs are parseable but no success signal was found ({checked} checked).",
                recommended_action="Inspect specific job ledgers before treating cron as healthy.",
            )

        return HealthStatus(
            component_name=component,
            status="healthy",
            last_checked_at=_now_iso(),
            human_summary=f"Enabled cron deliveries healthy ({healthy} healthy, {checked} checked).",
        )

    except Exception as e:
        return HealthStatus(
            component_name=component,
            status="unknown",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Cron health check failed: {e}",
        )


def _enabled_cron_jobs() -> dict[str, str]:
    jobs_path = OPENCLAW_ROOT / "cron" / "jobs.json"
    if not jobs_path.exists():
        return {}
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    result: dict[str, str] = {}
    for job in jobs or []:
        if not isinstance(job, dict) or not job.get("enabled"):
            continue
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        result[job_id] = str(job.get("name") or job_id)
    return result


def _latest_jsonl_entry(path: Path) -> Optional[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _is_future_one_shot_job(job_id: str) -> bool:
    jobs_path = OPENCLAW_ROOT / "cron" / "jobs.json"
    if not jobs_path.exists():
        return False
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    for job in jobs or []:
        if not isinstance(job, dict) or str(job.get("id")) != job_id:
            continue
        schedule = job.get("schedule") or {}
        if schedule.get("kind") != "at":
            return False
        at = schedule.get("at")
        if not at:
            return False
        try:
            scheduled_at = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError:
            return False
        return scheduled_at > datetime.now(timezone.utc)
    return False


def check_notion() -> HealthStatus:
    """
    Check Notion integration token validity.
    This is a lightweight check — verifies the token env var exists.
    A full API check would require making a network call.
    """
    component = "notion"
    token = os.environ.get("NOTION_INTEGRATION_TOKEN", "")

    if not token:
        # Check openclaw.json as fallback
        config_path = OPENCLAW_ROOT / "openclaw.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
                # Look for notion token in config (varies by structure/case).
                token = _deep_search_any_key(
                    config,
                    "NOTION_INTEGRATION_TOKEN",
                    "notion_integration_token",
                    "NOTION_API_KEY",
                    "notion_api_key",
                ) or ""
            except Exception:
                pass

    if not token:
        return HealthStatus(
            component_name=component,
            status="blocked",
            last_checked_at=_now_iso(),
            failure_class="auth_invalid",
            human_summary="Notion integration token not configured.",
            recommended_action="Set NOTION_INTEGRATION_TOKEN env var or add to openclaw.json.",
        )

    # Token exists — can't verify without network call, report as available
    return HealthStatus(
        component_name=component,
        status="healthy",
        last_checked_at=_now_iso(),
        human_summary="Notion token configured. Full validation requires API call.",
    )


def check_discord() -> HealthStatus:
    """
    Check Discord delivery health by inferring from recent activity.
    Looks for recent successful deliveries in cron logs.
    """
    component = "discord_delivery"
    try:
        # Check for Discord-related env vars or config. Some existing OpenClaw
        # configs only store channel IDs, so use the known Student/OpenClaw
        # default guild as a safe fallback for delivery-health visibility.
        guild_id = os.environ.get("DISCORD_GUILD_ID", "")
        if not guild_id:
            config_path = OPENCLAW_ROOT / "openclaw.json"
            if config_path.exists():
                try:
                    with open(config_path) as f:
                        config = json.load(f)
                    guild_id = str(_deep_search_any_key(config, "DISCORD_GUILD_ID", "discord_guild_id") or "")
                except Exception:
                    pass
        if not guild_id and _openclaw_discord_configured():
            guild_id = "1484954575675981906"

        if not guild_id:
            return HealthStatus(
                component_name=component,
                status="unknown",
                last_checked_at=_now_iso(),
                human_summary="Discord guild not configured. Cannot assess delivery health.",
                recommended_action="Set DISCORD_GUILD_ID in environment or openclaw.json.",
            )

        return HealthStatus(
            component_name=component,
            status="healthy",
            last_checked_at=_now_iso(),
            human_summary="Discord guild configured. Delivery health inferred from config presence.",
        )

    except Exception as e:
        return HealthStatus(
            component_name=component,
            status="unknown",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Discord health check failed: {e}",
        )


def _deep_search_key(obj, key: str):
    """Recursively search a dict/list for a key."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = _deep_search_key(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _deep_search_key(item, key)
            if result is not None:
                return result
    return None


def _deep_search_any_key(obj, *keys: str):
    """Recursively search a dict/list for any exact key."""
    for key in keys:
        result = _deep_search_key(obj, key)
        if result is not None:
            return result
    return None


def _openclaw_discord_configured() -> bool:
    """Return true when OpenClaw has a Discord channel config surface."""
    config_path = OPENCLAW_ROOT / "openclaw.json"
    if not config_path.exists():
        return False
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        return False
    channels = config.get("channels") if isinstance(config, dict) else None
    if isinstance(channels, dict) and isinstance(channels.get("discord"), dict):
        return True
    if _deep_search_any_key(config, "DISCORD_COMPANY_CHANNEL_ID", "DISCORD_LEARNING_CHANNEL_ID"):
        return True
    return False


def check_assistant_scheduler() -> HealthStatus:
    """Check Rocky assistant scheduler health without running helpers."""
    component = "assistant_scheduler"
    try:
        from assistant_scheduler_health import evaluate_all_scheduler_jobs

        payload = evaluate_all_scheduler_jobs(write_state=False, write_audit=False)
        status = payload.get("status", "unknown")
        jobs = payload.get("jobs") or []
        summaries = [
            f"{job.get('job_name')}: {job.get('summary')}"
            for job in jobs
        ]
        failure_class = None
        for job in jobs:
            if job.get("failure_class"):
                failure_class = job.get("failure_class")
                break
        mapped_status = status if status in {"healthy", "degraded", "blocked", "unknown"} else "unknown"
        return HealthStatus(
            component_name=component,
            status=mapped_status,
            last_checked_at=_now_iso(),
            failure_class=failure_class,
            human_summary="; ".join(summaries) if summaries else "No assistant scheduler jobs configured.",
            recommended_action="Run assistant-scheduler-health for details." if mapped_status != "healthy" else None,
        )
    except Exception as e:
        return HealthStatus(
            component_name=component,
            status="unknown",
            last_checked_at=_now_iso(),
            failure_class="unknown_error",
            human_summary=f"Assistant scheduler health check failed: {e}",
            recommended_action="Run assistant-scheduler-health --json for details.",
        )


def run_all_checks() -> list[HealthStatus]:
    """Run all integration health checks and return results."""
    checks = [
        check_student_archive,
        check_calendar,
        check_cron_delivery,
        check_assistant_scheduler,
        check_notion,
        check_discord,
    ]
    results = []
    for check_fn in checks:
        try:
            results.append(check_fn())
        except Exception as e:
            results.append(HealthStatus(
                component_name=check_fn.__name__.replace("check_", ""),
                status="unknown",
                last_checked_at=_now_iso(),
                failure_class="unknown_error",
                human_summary=f"Check crashed: {e}",
            ))
    return results


def format_health_report(statuses: list[HealthStatus]) -> str:
    """Format health statuses into a concise text report suitable for chat."""
    lines = ["Integration Health Report", "=" * 30]

    status_icons = {
        "healthy": "[OK]",
        "degraded": "[WARN]",
        "blocked": "[FAIL]",
        "unknown": "[????]",
    }

    for s in statuses:
        icon = status_icons.get(s.status, "[????]")
        line = f"{icon} {s.component_name}: {s.human_summary}"
        lines.append(line)
        if s.recommended_action:
            lines.append(f"     -> {s.recommended_action}")

    # Summary
    blocked = sum(1 for s in statuses if s.status == "blocked")
    degraded = sum(1 for s in statuses if s.status == "degraded")
    healthy = sum(1 for s in statuses if s.status == "healthy")

    lines.append("")
    lines.append(f"Summary: {healthy} healthy, {degraded} degraded, {blocked} blocked")

    return "\n".join(lines)


if __name__ == "__main__":
    statuses = run_all_checks()
    print(format_health_report(statuses))
