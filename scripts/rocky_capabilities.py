#!/usr/bin/env python3
"""
Rocky Capability Discoverability Helper

Classifies Rocky's current capabilities and maps common errors to
concrete next actions.

Usage:
    from rocky_capabilities import get_capability_snapshot, map_error_to_action, format_capability_report
    snapshot = get_capability_snapshot()
    print(format_capability_report(snapshot))

    # When an error occurs:
    result = map_error_to_action("Invalid token: The Notion integration token expired")
    print(result["human_summary"])
    print(result["recommended_action"])

Disable: simply don't import/call this module. No side effects.
"""

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


FIXTURES_PATH = Path(__file__).parent.parent / "improvement" / "fixtures" / "capability_cases.json"


@dataclass
class CapabilityState:
    """State of a single capability group."""
    group: str
    status: str  # "available" | "gated" | "degraded" | "blocked" | "unknown"
    detail: Optional[str] = None
    blocker: Optional[str] = None
    recommended_action: Optional[str] = None


@dataclass
class ErrorMapping:
    """Result of mapping an error to a capability state and next action."""
    capability_group: str
    mapped_status: str
    human_summary: str
    recommended_action: str


# Built-in failure-to-action mappings
# These are compiled from capability_cases.json + additional patterns
FAILURE_MAPPINGS = [
    {
        "pattern": r"(?i)(approval.*client|exec.*approval|not.*approved)",
        "group": "exec_approvals",
        "status": "gated",
        "summary": "This action requires exec approval, but the approval client isn't available in this session.",
        "action": "Use the exec-approvals gate: check ~/.openclaw/exec-approvals.json for pending approvals, or run from a session with approval client access.",
    },
    {
        "pattern": r"(?i)(notion.*invalid.*token|invalid.*notion.*token|notion.*401|notion.*unauthorized|notion.*token.*expired)",
        "group": "messaging_delivery",
        "status": "blocked",
        "summary": "Notion integration token is invalid or expired.",
        "action": "Re-authenticate: generate a new Notion integration token and update NOTION_INTEGRATION_TOKEN in the environment or openclaw.json.",
    },
    {
        "pattern": r"(?i)(rate.?limit|too many requests|429|retry after)",
        "group": "archive_search_ingest",
        "status": "degraded",
        "summary": "Rate-limited by the source API.",
        "action": "Wait for the cooldown period (usually 5 minutes), then retry. If persistent, check if API quota needs upgrading.",
    },
    {
        "pattern": r"(?i)(tree.*scoped|session.*visibility|cannot.*send.*to.*session|not.*visible.*from.*tree)",
        "group": "subagents",
        "status": "gated",
        "summary": "Cannot communicate with the target session due to tree-scoped visibility.",
        "action": "Use sessions_spawn instead of sessions_send. Spawned sessions have their own scope and can operate independently.",
    },
    {
        "pattern": r"(?i)(applescript.*time[d\s]*.?out|calendar.*time[d\s]*.?out|osascript.*error|calendar.*not.*respond)",
        "group": "calendar_mail",
        "status": "degraded",
        "summary": "Apple Calendar access via AppleScript is timing out.",
        "action": "Fall back to direct Calendar SQLite DB read at ~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb. Or use the Google Calendar MCP.",
    },
    {
        "pattern": r"(?i)(discord.*401|discord.*unauthorized|discord.*forbidden|discord.*token.*invalid)",
        "group": "messaging_delivery",
        "status": "blocked",
        "summary": "Discord delivery is blocked: authentication failed.",
        "action": "Check the Discord bot token in environment/config. Regenerate if expired.",
    },
    {
        "pattern": r"(?i)(cron.*fail|scheduled.*task.*fail|job.*not.*deliver)",
        "group": "cron",
        "status": "degraded",
        "summary": "Cron job executed but delivery to the target system failed.",
        "action": "Check the run ledger for the specific delivery failure. The job ran locally; the issue is downstream delivery.",
    },
    {
        "pattern": r"(?i)(fetch.*fail|web.*search.*unavailable|connection.*refused|dns.*resolution|econnrefused)",
        "group": "web_search_fetch",
        "status": "blocked",
        "summary": "Web fetch/search is not working.",
        "action": "Check network connectivity. If behind a proxy, verify proxy configuration. Try again in a few minutes.",
    },
    {
        "pattern": r"(?i)(config.*edit.*approval|cannot.*modify.*config|config.*protected)",
        "group": "config_editing",
        "status": "gated",
        "summary": "Config editing requires explicit operator approval.",
        "action": "Ask Dusan for explicit permission to modify the specific config value. Config changes are logged in config-audit.jsonl.",
    },
    {
        "pattern": r"(?i)(permission.*denied|403.*forbidden|access.*denied|insufficient.*permission)",
        "group": "exec_approvals",
        "status": "blocked",
        "summary": "Operation blocked: insufficient permissions.",
        "action": "Check which permission is needed and request access from the system administrator or operator.",
    },
    {
        "pattern": r"(?i)(401|unauthorized|auth.*fail|auth.*invalid|auth.*expired|token.*invalid|token.*expired)",
        "group": "messaging_delivery",
        "status": "blocked",
        "summary": "Authentication failed.",
        "action": "Check and refresh the relevant auth token/credentials.",
    },
    {
        "pattern": r"(?i)(student.*sqlite|student.*db|archive.*db|sqlite.*error)",
        "group": "archive_search_ingest",
        "status": "blocked",
        "summary": "Student archive database error.",
        "action": "Check SQLite DB at ~/.openclaw/student/student.sqlite3. Verify file exists and has correct permissions.",
    },
]


def map_error_to_action(error_text: str) -> Optional[ErrorMapping]:
    """
    Map an error message to a capability state, human summary, and next action.

    Args:
        error_text: The error message or text to classify

    Returns:
        ErrorMapping if a match is found, None otherwise
    """
    for mapping in FAILURE_MAPPINGS:
        if re.search(mapping["pattern"], error_text):
            return ErrorMapping(
                capability_group=mapping["group"],
                mapped_status=mapping["status"],
                human_summary=mapping["summary"],
                recommended_action=mapping["action"],
            )
    return None


def get_capability_snapshot(
    health_statuses: Optional[list] = None,
) -> list[CapabilityState]:
    """
    Get a snapshot of all capability groups' current state.

    Args:
        health_statuses: Optional list of HealthStatus objects from rocky_healthcheck.
                         If provided, uses them to populate capability states.

    Returns:
        List of CapabilityState objects for all groups
    """
    groups = [
        "exec_approvals",
        "messaging_delivery",
        "subagents",
        "cron",
        "calendar_mail",
        "archive_search_ingest",
        "web_search_fetch",
        "config_editing",
    ]

    # Start with unknown for all
    snapshot = {g: CapabilityState(group=g, status="unknown") for g in groups}

    # If health statuses provided, map them to capabilities
    if health_statuses:
        health_map = {
            "student_archive": "archive_search_ingest",
            "calendar": "calendar_mail",
            "cron_delivery": "cron",
            "notion": "messaging_delivery",
            "discord_delivery": "messaging_delivery",
        }
        for hs in health_statuses:
            cap_group = health_map.get(hs.component_name)
            if cap_group and cap_group in snapshot:
                # Map health status to capability status
                status_map = {
                    "healthy": "available",
                    "degraded": "degraded",
                    "blocked": "blocked",
                    "unknown": "unknown",
                }
                mapped = status_map.get(hs.status, "unknown")
                # Only downgrade, don't upgrade (worst-case wins for same group)
                priority = {"blocked": 0, "degraded": 1, "unknown": 2, "available": 3}
                current = snapshot[cap_group].status
                if current == "unknown" or priority.get(mapped, 2) < priority.get(current, 2):
                    snapshot[cap_group] = CapabilityState(
                        group=cap_group,
                        status=mapped,
                        detail=hs.human_summary,
                        blocker=hs.failure_class,
                        recommended_action=hs.recommended_action,
                    )

    return list(snapshot.values())


def format_capability_report(snapshot: list[CapabilityState]) -> str:
    """Format capability snapshot into a concise chat-friendly report."""
    lines = ["Capability Status", "=" * 25]

    status_icons = {
        "available": "[OK]",
        "gated": "[GATE]",
        "degraded": "[WARN]",
        "blocked": "[FAIL]",
        "unknown": "[????]",
    }

    group_display = {
        "exec_approvals": "Exec / Approvals",
        "messaging_delivery": "Messaging / Delivery",
        "subagents": "Subagents",
        "cron": "Cron Jobs",
        "calendar_mail": "Calendar / Mail",
        "archive_search_ingest": "Archive Search / Ingest",
        "web_search_fetch": "Web Search / Fetch",
        "config_editing": "Config Editing",
    }

    for cap in snapshot:
        icon = status_icons.get(cap.status, "[????]")
        name = group_display.get(cap.group, cap.group)
        line = f"{icon} {name}: {cap.status}"
        if cap.detail:
            line += f" — {cap.detail}"
        lines.append(line)
        if cap.recommended_action and cap.status in ("degraded", "blocked", "gated"):
            lines.append(f"     -> {cap.recommended_action}")

    # Summary
    available = sum(1 for c in snapshot if c.status == "available")
    issues = sum(1 for c in snapshot if c.status in ("degraded", "blocked", "gated"))
    unknown = sum(1 for c in snapshot if c.status == "unknown")

    lines.append("")
    lines.append(f"Summary: {available} available, {issues} with issues, {unknown} unknown")

    return "\n".join(lines)


if __name__ == "__main__":
    # Demo: run with health check integration
    try:
        from rocky_healthcheck import run_all_checks
        health = run_all_checks()
        snapshot = get_capability_snapshot(health_statuses=health)
    except ImportError:
        snapshot = get_capability_snapshot()

    print(format_capability_report(snapshot))
    print()

    # Demo: error mapping
    test_errors = [
        "Invalid token: The provided Notion integration token is invalid or has expired.",
        "Rate limit exceeded. Please retry after 300 seconds.",
        "AppleScript execution timed out after 5000ms",
        "Cannot send message to session: target session is not visible from this tree scope.",
        "HTTP 401 Unauthorized",
    ]
    print("Error Mapping Demo")
    print("=" * 25)
    for err in test_errors:
        result = map_error_to_action(err)
        if result:
            print(f"Error: {err[:60]}...")
            print(f"  -> [{result.mapped_status}] {result.human_summary}")
            print(f"  -> Action: {result.recommended_action}")
            print()
