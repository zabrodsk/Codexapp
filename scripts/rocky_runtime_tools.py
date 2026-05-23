#!/usr/bin/env python3
"""
Rocky Runtime Tools — thin orchestration entrypoint.

Subcommands:
  health-report            Run all integration checks and print a chat-ready report.
  capability-report        Run health checks, map to capability groups, print report.
  response-check-fixtures  Run ResponseChecker against improvement/fixtures/response_cases.json.
  show-recent-runs         Print a summary of recent run ledger entries.
  show-bob-jobs            Print recent durable Bob job states.
  show-bob-reports         Print recent durable Bob Discord report states.
  report-bob-stage         Post or dry-run one Bob stage update from durable state.
  mark-bob-dusan-notified  Mark that Rocky notified Dusan about a Bob job.
  archive-summary          Summarize recent Student archive items for a topic.
  runtime-version          Report deployed Rocky runtime helper provenance.
  runtime-answer           Answer one prompt through Rocky's read-only runtime memory path.
  work-signal-summary      Collect metadata-only Apple Mail/Calendar work signals.
  lesson-capture           Append one structured improvement lesson.
  lesson-recent            Show recent structured improvement lessons.
  memory-promote           Promote one durable cross-agent memory candidate into Rocky's Obsidian vault.
  memory-recent-promotions Show recent durable-memory promotion events.
  obsidian-status          Show Layer 3 Obsidian vault/QMD status.
  obsidian-sync            Ensure the Obsidian vault is configured and indexed.
  obsidian-query           Search Obsidian Layer 3 notes through QMD.
  obsidian-write           Safely create or append a note in the dedicated vault folder.

These subcommands call existing modules; they do not replace them.
Bob report commands write only Rocky-owned Bob report state unless live Discord
posting is explicitly requested with report-bob-stage without --dry-run.
Exit code 1 from health-report or capability-report means at least one component is blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
STUDENT_ROOT = ROOT.parent / "workspace-student"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(STUDENT_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDENT_ROOT))

from rocky_healthcheck import run_all_checks, format_health_report
from rocky_capabilities import get_capability_snapshot, format_capability_report
from rocky_response_checker import check_response_from_fixture
from rocky_run_ledger import RunLedger
from bob_job_runner import BobJobLedger, format_bob_jobs
from bob_job_reporting import (
    BOB_REPORT_STAGES,
    BobReportLedger,
    derive_stage_from_job,
    format_bob_reports,
    report_bob_stage,
)
from subagent_failure_events import SubagentFailureLedger, render_validation_result, validation_level
from subagent_recovery import recover_pending_failures
from rocky_improvement import capture_lesson, format_recent_lessons, read_recent_lessons
from obsidian_memory import (
    format_obsidian_query,
    format_obsidian_status,
    format_recent_promotions,
    get_obsidian_status,
    promote_cross_agent_memory,
    query_obsidian_vault,
    read_recent_promotions,
    sync_obsidian_vault,
    write_obsidian_note,
)
from assistant_audit_log import AssistantAuditLog
from agentmail_bridge_health import build_agentmail_bridge_health
from assistant_calendar_dry_run import build_calendar_dry_run
from assistant_calendar_policy import POLICY_VERSION, evaluate_calendar_policy
from assistant_calendar_status import (
    calendar_write_health,
    inspect_calendar_block,
    reconcile_calendar_blocks,
)
from assistant_calendar_tcc_probe import build_calendar_tcc_probe
from assistant_calendar_writer import create_calendar_block, delete_calendar_block
from assistant_codex_llm import task_llm_health
from assistant_notification_dispatcher import dispatch_failure_notification
from assistant_run_lock import smoke_lock_cycle
from assistant_scheduler_health import evaluate_all_scheduler_jobs, format_scheduler_health_report
from assistant_scheduler_state import AssistantSchedulerState
from coding_focus_live_booking import book_coding_focus_proposal
from coding_focus_proposal_engine import build_coding_focus_proposals
from coding_memory_enricher import enrich_project_memory
from coding_session_inspector import inspect_coding_signals
from coding_signal_sync import run_sync as run_coding_signal_sync
from coding_work_briefing_builder import build_coding_work_briefing
from coding_work_scheduler import run_coding_work_scheduler
from email_triage_live_booking import book_email_triage_proposal
from email_triage_proposal_engine import build_email_triage_proposals
from email_triage_scheduler import run_email_triage_scheduler
from notion_task_manager import (
    ensure_task_database_schema,
    list_open_tasks,
    notion_task_health,
    upsert_task,
)
from meeting_task_signal_reader import collect_meeting_task_signals
from task_deduper import dedupe_task_candidates
from task_identity_resolver import resolve_task_identities
from task_lifecycle_engine import run_task_lifecycle
from task_command_capture_scheduler import run_task_command_capture_scheduler
from task_command_interpreter import apply_task_command
from task_detector import detect_task_candidates
from task_focus_live_booking import book_task_focus_proposal
from task_focus_proposal_engine import build_task_focus_proposals
from task_reminder_engine import run_task_reminders
from task_signal_collector import build_manual_task_signal, collect_task_signals
from task_spine_scheduler import run_task_spine_scheduler
from training_calendar_live_booking import book_training_calendar_proposal
from training_calendar_proposal_engine import build_training_calendar_proposals
from training_calendar_reconciler import reconcile_training_calendar
from training_calendar_scheduler import run_training_calendar_scheduler
from trainingpeaks_ics_reader import preview_ics_file, preview_webcal_url_file
from trainingpeaks_read_path_probe import probe_trainingpeaks_read_paths

if TYPE_CHECKING:
    from student.service import StudentService

DEFAULT_FIXTURE_PATH = ROOT / "improvement" / "fixtures" / "response_cases.json"
ROCKY_RUNTIME_TOOL_NAME = "rocky-runtime-tools"
ROCKY_RUNTIME_TOOL_VERSION = "0.1.0"
ROCKY_RUNTIME_SOURCE_REPO = "https://github.com/zabrodsk/rocky-runtime-tools"
ROCKY_RUNTIME_TARGET = "/Users/clawdbot/.openclaw/workspace"
RUNTIME_DEPLOY_MANIFEST_PATH = ROOT / "deploy" / "runtime-manifest.json"
RUNTIME_DEPLOYABLE_FILES = (
    "scripts/rocky_runtime_tools.py",
    "scripts/assistant_audit_log.py",
    "scripts/agentmail_bridge_health.py",
    "scripts/agentmail_bridge_deploy.py",
    "scripts/assistant_calendar_dry_run.py",
    "scripts/assistant_calendar_policy.py",
    "scripts/assistant_calendar_state.py",
    "scripts/assistant_calendar_status.py",
    "scripts/assistant_calendar_tcc_probe.py",
    "scripts/assistant_calendar_writer.py",
    "scripts/assistant_codex_llm.py",
    "scripts/assistant_launchd.py",
    "scripts/assistant_notification_dispatcher.py",
    "scripts/assistant_run_lock.py",
    "scripts/assistant_scheduler_health.py",
    "scripts/assistant_scheduler_health_launcher.py",
    "scripts/assistant_scheduler_state.py",
    "scripts/coding_signal_sync.py",
    "scripts/coding_session_inspector.py",
    "scripts/coding_repo_inspector.py",
    "scripts/coding_memory_enricher.py",
    "scripts/coding_work_briefing_builder.py",
    "scripts/coding_focus_proposal_engine.py",
    "scripts/coding_focus_live_booking.py",
    "scripts/coding_work_scheduler.py",
    "scripts/email_triage_live_booking.py",
    "scripts/email_triage_proposal_engine.py",
    "scripts/email_triage_reader.py",
    "scripts/email_triage_scheduler.py",
    "scripts/email_triage_time_estimator.py",
    "scripts/notion_task_manager.py",
    "scripts/meeting_task_signal_reader.py",
    "scripts/discord_task_command_reader.py",
    "scripts/email_task_command_reader.py",
    "scripts/task_command_capture_scheduler.py",
    "scripts/task_command_interpreter.py",
    "scripts/task_deduper.py",
    "scripts/task_detector.py",
    "scripts/task_focus_live_booking.py",
    "scripts/task_identity_resolver.py",
    "scripts/task_lifecycle_engine.py",
    "scripts/task_focus_proposal_engine.py",
    "scripts/task_reminder_engine.py",
    "scripts/task_signal_collector.py",
    "scripts/task_spine_scheduler.py",
    "skills/notion-task-manager/SKILL.md",
    "skills/task-command-capture/SKILL.md",
    "skills/meeting-task-capture/SKILL.md",
    "skills/discord-task-capture/SKILL.md",
    "skills/email-command-capture/SKILL.md",
    "skills/task-deduper/SKILL.md",
    "skills/task-detector/SKILL.md",
    "skills/task-focus-calendar/SKILL.md",
    "skills/task-lifecycle-engine/SKILL.md",
    "skills/task-reminder-engine/SKILL.md",
    "skills/coding-session-inspector/SKILL.md",
    "skills/coding-memory-enricher/SKILL.md",
    "skills/coding-work-briefing/SKILL.md",
    "skills/coding-focus-calendar/SKILL.md",
    "scripts/training_calendar_live_booking.py",
    "scripts/training_calendar_proposal_engine.py",
    "scripts/training_calendar_reconciler.py",
    "scripts/training_calendar_scheduler.py",
    "scripts/trainingpeaks_ics_reader.py",
    "scripts/trainingpeaks_read_path_probe.py",
    "services/agentmail-bridge/bridge.mjs",
    "services/agentmail-bridge/test_email_security_bridge.mjs",
    "services/agentmail-bridge/package.json",
    "services/agentmail-bridge/package-lock.json",
    "services/agentmail-bridge/config.example.json",
    "services/agentmail-bridge/launchagents/ai.openclaw.agentmail-bridge.plist.template",
    "services/email-security/email_security.mjs",
    "services/email-security/rules.json",
    "docs/agentmail-bridge-runbook.md",
    "tests/test_runtime_integration.py",
)
RUNTIME_ANSWER_CONTEXT_LIMIT = 5
RUNTIME_ANSWER_ARCHIVES = ("company", "knowladge")
RUNTIME_ANSWER_MAX_CONTEXT_CHARS = 6000
RUNTIME_ANSWER_MAX_SOURCE_TEXT_CHARS = 260
RUNTIME_ANSWER_LLM_TIMEOUT_SECONDS = 8
WORK_SIGNAL_TIMEZONE = "Europe/Prague"
WORK_SIGNAL_SOURCES = {
    "apple-mail": "apple_mail",
    "apple_mail": "apple_mail",
    "apple-calendar": "apple_calendar",
    "apple_calendar": "apple_calendar",
}
WORK_SIGNAL_RAW_FIELDS_EXCLUDED = [
    "email body",
    "subject",
    "sender",
    "recipient",
    "attendee",
    "event title",
    "event description",
    "invite body",
    "location",
    "notes",
]
BETTY_PYTHON = Path("/Users/clawdbot/.openclaw/workspace-betty/.venv/bin/python")
BETTY_WEEKLY_REPORT_HELPER = Path(
    "/Users/clawdbot/.openclaw/workspace-betty/skills/weekly-boss-report/scripts/weekly_report_helper.py"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rocky runtime tools — health, capability, response QA, run history."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "health-report",
        help="Run all integration health checks and print a report.",
    )

    sub.add_parser(
        "capability-report",
        help="Run health checks, map to capability groups, print capability report.",
    )

    rc = sub.add_parser(
        "response-check-fixtures",
        help="Run ResponseChecker against the response fixture file.",
    )
    rc.add_argument(
        "--fixture-path",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Path to response fixture JSON file (default: improvement/fixtures/response_cases.json).",
    )
    rc.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON results instead of a human-readable summary.",
    )

    rr = sub.add_parser(
        "show-recent-runs",
        help="Print a summary of recent run ledger entries.",
    )
    rr.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent runs to show (default: 10).",
    )
    rr.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable table.",
    )

    bj = sub.add_parser(
        "show-bob-jobs",
        help="Print recent durable Bob job states.",
    )
    bj.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent Bob jobs to show (default: 10).",
    )
    bj.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    br = sub.add_parser(
        "show-bob-reports",
        help="Print recent durable Bob Discord report states.",
    )
    br.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent Bob reports to show (default: 10).",
    )
    br.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    report_bob = sub.add_parser(
        "report-bob-stage",
        help="Post or dry-run one compact Bob stage update from durable job state.",
    )
    report_bob.add_argument("--job-id", required=True, help="Bob job id to report.")
    report_bob.add_argument(
        "--stage",
        choices=BOB_REPORT_STAGES,
        help="Stage to report. Defaults to the current stage derived from process_status.",
    )
    report_bob.add_argument("--summary", help="Optional concise stage summary.")
    report_bob.add_argument("--plan-path", help="Optional plan artifact path for plan-ready reports.")
    report_bob.add_argument(
        "--artifact-path",
        action="append",
        default=[],
        help="Optional extra artifact path. May be passed multiple times.",
    )
    report_bob.add_argument(
        "--force",
        action="store_true",
        help="Post even if this job/stage was already posted.",
    )
    report_bob.add_argument(
        "--dry-run",
        action="store_true",
        help="Render and record the report without live Discord delivery.",
    )
    report_bob.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    mark_bob = sub.add_parser(
        "mark-bob-dusan-notified",
        help="Mark that Rocky notified Dusan about a Bob job.",
    )
    mark_bob.add_argument("--job-id", required=True, help="Bob job id to update.")
    mark_bob.add_argument("--note", help="Optional note about the notification.")
    mark_bob.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    failures = sub.add_parser(
        "show-subagent-failures",
        help="Print recent structured subagent failure events.",
    )
    failures.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent latest-state events to show (default: 10).",
    )
    failures.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    recover = sub.add_parser(
        "recover-subagent-failures",
        help="Attempt safe recovery for unresolved subagent failures and report incidents to #general.",
    )
    recover.add_argument(
        "--limit",
        type=int,
        help="Maximum number of unresolved events to process.",
    )
    recover.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    archive = sub.add_parser(
        "archive-summary",
        help="Summarize recent Student archive items for a topic.",
    )
    archive.add_argument("query", help="Topic/query to retrieve from the archive.")
    archive.add_argument(
        "--archive",
        choices=["knowladge", "company"],
        default="knowladge",
        help="Archive to search (default: knowladge).",
    )
    archive.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of source items to include (default: 5).",
    )
    archive.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    audit_recent = sub.add_parser(
        "assistant-audit-recent",
        help="Show recent assistant action audit events.",
    )
    audit_recent.add_argument("--limit", type=int, default=20, help="Number of audit events to show.")
    audit_recent.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    audit_recent.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    policy_check = sub.add_parser(
        "calendar-policy-check",
        help="Evaluate Rocky proactive calendar booking policy without writing calendar events.",
    )
    policy_check.add_argument("--kind", required=True, help="Block kind: training, email_triage, coding_focus, task_focus.")
    policy_check.add_argument("--date", required=True, help="Local date in YYYY-MM-DD format.")
    policy_check.add_argument("--start", required=True, help="Local start time in HH:MM format.")
    policy_check.add_argument("--duration-minutes", required=True, type=int, dest="duration_minutes")
    policy_check.add_argument("--label", help="Optional user-facing block label.")
    policy_check.add_argument("--source-ref", action="append", default=[], dest="source_ref", help="Optional source reference.")
    policy_check.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    policy_check.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    dry_run = sub.add_parser(
        "calendar-dry-run",
        help="Build a dry-run Rocky calendar proposal without writing calendar events.",
    )
    dry_run.add_argument("--kind", required=True, help="Block kind: training, email_triage, coding_focus, task_focus.")
    dry_run.add_argument("--date", required=True, help="Local date in YYYY-MM-DD format.")
    dry_run.add_argument("--window-start", required=True, dest="window_start", help="Local window start in HH:MM format.")
    dry_run.add_argument("--window-end", required=True, dest="window_end", help="Local window end in HH:MM format.")
    dry_run.add_argument("--duration-minutes", required=True, type=int, dest="duration_minutes")
    dry_run.add_argument("--label", help="Optional user-facing block label.")
    dry_run.add_argument("--reason", default="Rocky dry-run calendar proposal", help="Safe proposal reason.")
    dry_run.add_argument("--confidence", default="medium", help="Proposal confidence label.")
    dry_run.add_argument("--source-ref", action="append", default=[], dest="source_ref", help="Optional source reference.")
    dry_run.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    dry_run.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    dry_run.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    live_create = sub.add_parser(
        "calendar-block-create",
        help="Create a Rocky-owned Apple Calendar block only when --live is explicitly set.",
    )
    live_create.add_argument("--kind", required=True, help="Block kind: training, email_triage, coding_focus, task_focus.")
    live_create.add_argument("--date", required=True, help="Local date in YYYY-MM-DD format.")
    live_create.add_argument("--window-start", required=True, dest="window_start", help="Local window start in HH:MM format.")
    live_create.add_argument("--window-end", required=True, dest="window_end", help="Local window end in HH:MM format.")
    live_create.add_argument("--duration-minutes", required=True, type=int, dest="duration_minutes")
    live_create.add_argument("--label", help="Optional user-facing block label.")
    live_create.add_argument("--reason", default="Rocky live calendar block", help="Safe booking reason.")
    live_create.add_argument("--confidence", default="medium", help="Proposal confidence label.")
    live_create.add_argument("--source-ref", action="append", default=[], dest="source_ref", help="Optional source reference.")
    live_create.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    live_create.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    live_create.add_argument("--state-db", dest="state_db", help="Optional assistant calendar SQLite path.")
    live_create.add_argument("--scheduler-db", dest="scheduler_db", help="Optional assistant scheduler SQLite path for locks.")
    live_create.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    live_create.add_argument("--live", action="store_true", help="Required guard for live Apple Calendar writes.")
    live_create.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    live_delete = sub.add_parser(
        "calendar-block-delete",
        help="Delete a Rocky-owned Apple Calendar block by idempotency key only when --live is explicitly set.",
    )
    live_delete.add_argument("--idempotency-key", required=True, dest="idempotency_key", help="Rocky calendar block idempotency key.")
    live_delete.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    live_delete.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    live_delete.add_argument("--state-db", dest="state_db", help="Optional assistant calendar SQLite path.")
    live_delete.add_argument("--scheduler-db", dest="scheduler_db", help="Optional assistant scheduler SQLite path for locks.")
    live_delete.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    live_delete.add_argument("--live", action="store_true", help="Required guard for live Apple Calendar deletes.")
    live_delete.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    block_status = sub.add_parser(
        "calendar-block-status",
        help="Inspect Rocky calendar block state and Calendar presence without writing events.",
    )
    block_status.add_argument("--idempotency-key", required=True, dest="idempotency_key", help="Rocky calendar block idempotency key.")
    block_status.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    block_status.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    block_status.add_argument("--state-db", dest="state_db", help="Optional assistant calendar SQLite path.")
    block_status.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    reconcile = sub.add_parser(
        "calendar-block-reconcile",
        help="Read-only reconcile Rocky calendar state against Apple Calendar, optionally marking stale state.",
    )
    reconcile.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    reconcile.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    reconcile.add_argument("--state-db", dest="state_db", help="Optional assistant calendar SQLite path.")
    reconcile.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    reconcile.add_argument(
        "--mark-stale",
        action="store_true",
        dest="mark_stale",
        help="Mark active Rocky state rows stale when the Calendar event is missing.",
    )
    reconcile.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    write_health = sub.add_parser(
        "calendar-write-health",
        help="Check Calendar DB, EventKit, Swift, and AppleScript write prerequisites without creating events.",
    )
    write_health.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    write_health.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    write_health.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    write_health.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    tcc_probe = sub.add_parser(
        "calendar-tcc-probe",
        help="Run read-only Calendar/TCC permission diagnostics for the current execution context.",
    )
    tcc_probe.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    tcc_probe.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    tp_check = sub.add_parser(
        "trainingpeaks-read-path-check",
        help="Check read-only TrainingPeaks planned-workout source availability.",
    )
    tp_check.add_argument("--calendar-db-path", dest="calendar_db_path", help="Optional Apple Calendar SQLite path.")
    tp_check.add_argument("--events-db-path", dest="events_db_path", help="Optional OpenClaw events SQLite path.")
    tp_check.add_argument("--webcal-url-file", dest="webcal_url_file", help="Optional secret file containing the TrainingPeaks webcal URL.")
    tp_check.add_argument("--start-date", dest="start_date", help="Optional local start date in YYYY-MM-DD format.")
    tp_check.add_argument("--days-ahead", type=int, default=14, dest="days_ahead", help="Number of days to inspect (default: 14).")
    tp_check.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    tp_preview = sub.add_parser(
        "trainingpeaks-ics-preview",
        help="Preview normalized workouts from a TrainingPeaks .ics file or secret webcal URL file.",
    )
    tp_source = tp_preview.add_mutually_exclusive_group(required=True)
    tp_source.add_argument("--ics-file", dest="ics_file", help="Path to a local .ics file.")
    tp_source.add_argument("--webcal-url-file", dest="webcal_url_file", help="Secret file containing the TrainingPeaks webcal URL.")
    tp_preview.add_argument("--start-date", dest="start_date", help="Optional local start date in YYYY-MM-DD format.")
    tp_preview.add_argument("--days-ahead", type=int, default=14, dest="days_ahead", help="Number of days to preview (default: 14).")
    tp_preview.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    training_proposals = sub.add_parser(
        "training-calendar-proposals",
        help="Build dry-run TrainingPeaks-derived training calendar proposals.",
    )
    training_proposals.add_argument(
        "--webcal-url-file",
        default="/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url",
        dest="webcal_url_file",
        help="Secret file containing the TrainingPeaks webcal URL.",
    )
    training_proposals.add_argument("--planning-date", dest="planning_date", help="Optional local planning date in YYYY-MM-DD format.")
    training_proposals.add_argument("--target-working-days", type=int, default=3, dest="target_working_days", help="Working days ahead to target (default: 3).")
    training_proposals.add_argument("--days-ahead", type=int, default=14, dest="days_ahead", help="Number of TrainingPeaks days to preview (default: 14).")
    training_proposals.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    training_proposals.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    training_proposals.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    training_proposals.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    training_book = sub.add_parser(
        "training-calendar-book",
        help="Supervised live booking for one selected TrainingPeaks-derived training proposal.",
    )
    training_book.add_argument("--idempotency-key", required=True, dest="idempotency_key", help="Training proposal idempotency key to book.")
    training_book.add_argument(
        "--webcal-url-file",
        default="/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url",
        dest="webcal_url_file",
        help="Secret file containing the TrainingPeaks webcal URL.",
    )
    training_book.add_argument("--planning-date", dest="planning_date", help="Optional local planning date in YYYY-MM-DD format.")
    training_book.add_argument("--target-working-days", type=int, default=3, dest="target_working_days", help="Working days ahead to target (default: 3).")
    training_book.add_argument("--days-ahead", type=int, default=14, dest="days_ahead", help="Number of TrainingPeaks days to preview (default: 14).")
    training_book.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    training_book.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    training_book.add_argument("--state-db", dest="state_db", help="Optional assistant calendar SQLite path.")
    training_book.add_argument("--scheduler-db", dest="scheduler_db", help="Optional assistant scheduler SQLite path for locks.")
    training_book.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    training_book.add_argument("--live", action="store_true", help="Required guard for live Apple Calendar writes.")
    training_book.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    training_scheduler = sub.add_parser(
        "training-calendar-scheduler-run",
        help="Run automatic TrainingPeaks-derived training calendar booking scheduler.",
    )
    training_scheduler.add_argument(
        "--webcal-url-file",
        default="/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url",
        dest="webcal_url_file",
        help="Secret file containing the TrainingPeaks webcal URL.",
    )
    training_scheduler.add_argument("--planning-date", dest="planning_date", help="Optional local planning date in YYYY-MM-DD format.")
    training_scheduler.add_argument("--target-working-days", type=int, default=3, dest="target_working_days", help="Working days ahead to target (default: 3).")
    training_scheduler.add_argument("--days-ahead", type=int, default=14, dest="days_ahead", help="Number of TrainingPeaks days to preview (default: 14).")
    training_scheduler.add_argument("--calendar", default="Calendar", dest="calendar_name", help="Apple Calendar name (default: Calendar).")
    training_scheduler.add_argument("--max-bookings", type=int, default=1, dest="max_bookings", help="Maximum automatic bookings per run (default: 1).")
    training_scheduler.add_argument("--db-path", dest="db_path", help="Optional Apple Calendar SQLite path.")
    training_scheduler.add_argument("--calendar-state-db", dest="calendar_state_db", help="Optional assistant calendar SQLite path.")
    training_scheduler.add_argument("--scheduler-db", dest="scheduler_db", help="Optional assistant scheduler SQLite path.")
    training_scheduler.add_argument("--ledger-path", dest="ledger_path", help="Optional assistant audit JSONL path.")
    training_scheduler.add_argument(
        "--state-file",
        default="/Users/clawdbot/.openclaw/state/training_calendar_scheduler.json",
        dest="state_file",
        help="Safe scheduler state JSON path.",
    )
    training_scheduler.add_argument("--lock-ttl-seconds", type=int, default=1800, dest="lock_ttl_seconds", help="Duplicate-run lock TTL in seconds.")
    training_scheduler.add_argument("--live", action="store_true", help="Enable live Apple Calendar writes through existing booking rails.")
    training_scheduler.add_argument("--reconcile", action="store_true", help="Run TrainingPeaks/Calendar reconciliation after scheduler booking.")
    training_scheduler.add_argument("--fix-safe", action="store_true", dest="fix_safe", help="Apply narrowly safe reconciliation fixes when --live is set.")
    training_scheduler.add_argument("--notify-failures", action="store_true", dest="notify_failures", help="Send failure/manual-review notifications.")
    training_scheduler.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    training_scheduler.add_argument("--notification-channel-id", dest="notification_channel_id")
    training_scheduler.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    training_scheduler.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    training_reconcile = sub.add_parser(
        "training-calendar-reconcile",
        help="Reconcile current TrainingPeaks workouts with Rocky-owned Calendar blocks.",
    )
    training_reconcile.add_argument(
        "--webcal-url-file",
        default="/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url",
        dest="webcal_url_file",
        help="Secret file containing the TrainingPeaks webcal URL.",
    )
    training_reconcile.add_argument("--planning-date", dest="planning_date")
    training_reconcile.add_argument("--days-ahead", type=int, default=14, dest="days_ahead")
    training_reconcile.add_argument("--calendar", default="Calendar", dest="calendar_name")
    training_reconcile.add_argument("--db-path", dest="db_path")
    training_reconcile.add_argument("--state-db", dest="state_db")
    training_reconcile.add_argument("--scheduler-db", dest="scheduler_db")
    training_reconcile.add_argument("--ledger-path", dest="ledger_path")
    training_reconcile.add_argument("--fix-safe", action="store_true", dest="fix_safe")
    training_reconcile.add_argument("--live", action="store_true")
    training_reconcile.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    training_reconcile.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    training_reconcile.add_argument("--notification-channel-id", dest="notification_channel_id")
    training_reconcile.add_argument("--json", action="store_true", dest="json_output")

    email_proposals = sub.add_parser(
        "email-triage-proposals",
        help="Build dry-run same-day unread-email triage calendar proposals.",
    )
    email_proposals.add_argument("--planning-date", dest="planning_date")
    email_proposals.add_argument("--hours", type=int, default=168)
    email_proposals.add_argument("--limit", type=int, default=100)
    email_proposals.add_argument("--db-path", dest="db_path")
    email_proposals.add_argument("--ledger-path", dest="ledger_path")
    email_proposals.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    email_proposals.add_argument("--json", action="store_true", dest="json_output")

    email_book = sub.add_parser(
        "email-triage-book",
        help="Supervised live booking for one selected unread-email triage proposal.",
    )
    email_book.add_argument("--idempotency-key", required=True, dest="idempotency_key")
    email_book.add_argument("--planning-date", dest="planning_date")
    email_book.add_argument("--calendar", default="Calendar", dest="calendar_name")
    email_book.add_argument("--hours", type=int, default=168)
    email_book.add_argument("--limit", type=int, default=100)
    email_book.add_argument("--db-path", dest="db_path")
    email_book.add_argument("--state-db", dest="state_db")
    email_book.add_argument("--scheduler-db", dest="scheduler_db")
    email_book.add_argument("--ledger-path", dest="ledger_path")
    email_book.add_argument("--live", action="store_true")
    email_book.add_argument("--json", action="store_true", dest="json_output")

    email_scheduler = sub.add_parser(
        "email-triage-scheduler-run",
        help="Run automatic unread-email triage calendar booking scheduler.",
    )
    email_scheduler.add_argument("--planning-date", dest="planning_date")
    email_scheduler.add_argument("--calendar", default="Calendar", dest="calendar_name")
    email_scheduler.add_argument("--hours", type=int, default=168)
    email_scheduler.add_argument("--limit", type=int, default=100)
    email_scheduler.add_argument("--db-path", dest="db_path")
    email_scheduler.add_argument("--calendar-state-db", dest="calendar_state_db")
    email_scheduler.add_argument("--scheduler-db", dest="scheduler_db")
    email_scheduler.add_argument("--ledger-path", dest="ledger_path")
    email_scheduler.add_argument(
        "--state-file",
        default="/Users/clawdbot/.openclaw/state/email_triage_scheduler.json",
        dest="state_file",
    )
    email_scheduler.add_argument("--lock-ttl-seconds", type=int, default=1800, dest="lock_ttl_seconds")
    email_scheduler.add_argument("--live", action="store_true")
    email_scheduler.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    email_scheduler.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    email_scheduler.add_argument("--notification-channel-id", dest="notification_channel_id")
    email_scheduler.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    email_scheduler.add_argument("--json", action="store_true", dest="json_output")

    notion_health = sub.add_parser(
        "notion-task-health",
        help="Check Rocky's dedicated Notion task database configuration.",
    )
    notion_health.add_argument("--json", action="store_true", dest="json_output")

    notion_schema = sub.add_parser(
        "notion-task-schema-ensure",
        help="Dry-run or create/repair Rocky's dedicated Notion task database schema.",
    )
    notion_schema.add_argument("--live", action="store_true")
    notion_schema.add_argument("--json", action="store_true", dest="json_output")

    notion_list = sub.add_parser(
        "notion-task-list",
        help="List sanitized open Rocky tasks from Notion.",
    )
    notion_list.add_argument("--limit", type=int, default=20)
    notion_list.add_argument("--json", action="store_true", dest="json_output")

    task_detect = sub.add_parser(
        "task-detect",
        help="Collect task signals and detect candidate personal tasks.",
    )
    task_detect.add_argument("--source", action="append", dest="sources")
    task_detect.add_argument("--since-days", type=int, default=7)
    task_detect.add_argument("--limit", type=int, default=30)
    task_detect.add_argument("--no-llm", action="store_true", dest="no_llm")
    task_detect.add_argument("--json", action="store_true", dest="json_output")

    meeting_task_signals = sub.add_parser(
        "meeting-task-signals",
        help="Collect direct task signals from recent Rocky meeting action sections.",
    )
    meeting_task_signals.add_argument("--meeting-dir", dest="meeting_dir")
    meeting_task_signals.add_argument("--since-days", type=int, default=14)
    meeting_task_signals.add_argument("--limit", type=int, default=30)
    meeting_task_signals.add_argument("--json", action="store_true", dest="json_output")

    task_llm_health = sub.add_parser(
        "task-detector-llm-health",
        help="Check Rocky's task-detector Codex LLM path without writing Notion or Calendar.",
    )
    task_llm_health.add_argument("--json", action="store_true", dest="json_output")

    task_reminders = sub.add_parser(
        "task-reminders-run",
        help="Run task reminder selection and optional notification.",
    )
    task_reminders.add_argument("--today", dest="today")
    task_reminders.add_argument("--notify", action="store_true")
    task_reminders.add_argument("--live", action="store_true")
    task_reminders.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    task_reminders.add_argument("--notification-channel-id", dest="notification_channel_id")
    task_reminders.add_argument("--ledger-path", dest="ledger_path")
    task_reminders.add_argument("--scheduler-db", dest="scheduler_db")
    task_reminders.add_argument("--json", action="store_true", dest="json_output")


    task_lifecycle = sub.add_parser(
        "task-lifecycle-run",
        help="Run Rocky task lifecycle/reminder metadata updates.",
    )
    task_lifecycle.add_argument("--today", dest="today")
    task_lifecycle.add_argument("--live", action="store_true")
    task_lifecycle.add_argument("--ledger-path", dest="ledger_path")
    task_lifecycle.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    task_lifecycle.add_argument("--json", action="store_true", dest="json_output")

    task_focus = sub.add_parser(
        "task-focus-proposals",
        help="Build dry-run task focus calendar proposals from Notion tasks.",
    )
    task_focus.add_argument("--planning-date", dest="planning_date")
    task_focus.add_argument("--db-path", dest="db_path")
    task_focus.add_argument("--ledger-path", dest="ledger_path")
    task_focus.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    task_focus.add_argument("--json", action="store_true", dest="json_output")

    task_book = sub.add_parser(
        "task-focus-book",
        help="Supervised live booking for one selected Rocky task focus proposal.",
    )
    task_book.add_argument("--idempotency-key", required=True, dest="idempotency_key")
    task_book.add_argument("--planning-date", dest="planning_date")
    task_book.add_argument("--calendar", default="Calendar", dest="calendar_name")
    task_book.add_argument("--db-path", dest="db_path")
    task_book.add_argument("--state-db", dest="state_db")
    task_book.add_argument("--scheduler-db", dest="scheduler_db")
    task_book.add_argument("--ledger-path", dest="ledger_path")
    task_book.add_argument("--live", action="store_true")
    task_book.add_argument("--json", action="store_true", dest="json_output")

    task_command = sub.add_parser(
        "task-command-apply",
        help="Apply an explicit Discord/email-style task command into Notion.",
    )
    task_command.add_argument("--text", required=True)
    task_command.add_argument("--source", default="Command")
    task_command.add_argument("--source-ref", default="manual:command", dest="source_ref")
    task_command.add_argument("--live", action="store_true")
    task_command.add_argument("--no-llm", action="store_true", dest="no_llm")
    task_command.add_argument("--ledger-path", dest="ledger_path")
    task_command.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    task_command.add_argument("--json", action="store_true", dest="json_output")

    task_command_capture = sub.add_parser(
        "task-command-capture-run",
        help="Run Rocky's near-real-time Discord/email task command capture scheduler.",
    )
    task_command_capture.add_argument("--source", action="append", dest="sources")
    task_command_capture.add_argument("--live", action="store_true")
    task_command_capture.add_argument("--notify-failures", action="store_true", dest="notify_failures")
    task_command_capture.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    task_command_capture.add_argument("--notification-channel-id", dest="notification_channel_id")
    task_command_capture.add_argument("--since-minutes", type=int, default=10, dest="since_minutes")
    task_command_capture.add_argument("--limit", type=int, default=20)
    task_command_capture.add_argument("--scheduler-db", dest="scheduler_db")
    task_command_capture.add_argument("--ledger-path", dest="ledger_path")
    task_command_capture.add_argument("--state-file", default="/Users/clawdbot/.openclaw/state/task_command_capture_scheduler.json", dest="state_file")
    task_command_capture.add_argument("--lock-ttl-seconds", type=int, default=240, dest="lock_ttl_seconds")
    task_command_capture.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    task_command_capture.add_argument("--json", action="store_true", dest="json_output")

    task_scheduler = sub.add_parser(
        "task-spine-scheduler-run",
        help="Run Rocky's autonomous personal task spine scheduler.",
    )
    task_scheduler.add_argument("--planning-date", dest="planning_date")
    task_scheduler.add_argument("--live", action="store_true")
    task_scheduler.add_argument("--notify", action="store_true")
    task_scheduler.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    task_scheduler.add_argument("--notification-channel-id", dest="notification_channel_id")
    task_scheduler.add_argument("--source", action="append", dest="sources")
    task_scheduler.add_argument("--since-days", type=int, default=7)
    task_scheduler.add_argument("--limit", type=int, default=30)
    task_scheduler.add_argument("--db-path", dest="db_path")
    task_scheduler.add_argument("--calendar-state-db", dest="calendar_state_db")
    task_scheduler.add_argument("--scheduler-db", dest="scheduler_db")
    task_scheduler.add_argument("--ledger-path", dest="ledger_path")
    task_scheduler.add_argument(
        "--state-file",
        default="/Users/clawdbot/.openclaw/state/task_spine_scheduler.json",
        dest="state_file",
    )
    task_scheduler.add_argument("--lock-ttl-seconds", type=int, default=1800, dest="lock_ttl_seconds")
    task_scheduler.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    task_scheduler.add_argument("--json", action="store_true", dest="json_output")

    coding_sync = sub.add_parser(
        "coding-signal-sync",
        help="Build and optionally push sanitized laptop coding signals for Rocky.",
    )
    coding_sync.add_argument("--output-path", dest="output_path")
    coding_sync.add_argument("--remote-host", default="clawdbot-mini", dest="remote_host")
    coding_sync.add_argument("--remote-path", default="/Users/clawdbot/.openclaw/inbox/coding-signals/dusan-laptop/latest.json", dest="remote_path")
    coding_sync.add_argument("--limit", type=int, default=30)
    coding_sync.add_argument("--no-push", action="store_false", dest="push", default=True)
    coding_sync.add_argument("--json", action="store_true", dest="json_output")

    coding_inspect = sub.add_parser(
        "coding-signal-inspect",
        help="Inspect sanitized coding signals visible to Rocky.",
    )
    coding_inspect.add_argument("--laptop-manifest-path", dest="laptop_manifest_path")
    coding_inspect.add_argument("--no-local-sessions", action="store_false", dest="include_local_sessions", default=True)
    coding_inspect.add_argument("--no-repos", action="store_false", dest="include_repos", default=True)
    coding_inspect.add_argument("--limit", type=int, default=30)
    coding_inspect.add_argument("--json", action="store_true", dest="json_output")

    coding_memory = sub.add_parser(
        "coding-memory-enrich",
        help="Preview read-only Obsidian Layer 3 memory enrichment for one coding project.",
    )
    coding_memory.add_argument("--project", required=True)
    coding_memory.add_argument("--title", default="")
    coding_memory.add_argument("--limit", type=int, default=3)
    coding_memory.add_argument("--json", action="store_true", dest="json_output")

    coding_briefing = sub.add_parser(
        "coding-work-briefing",
        help="Build Rocky's noon coding work briefing from sanitized signals.",
    )
    coding_briefing.add_argument("--planning-date", dest="planning_date")
    coding_briefing.add_argument("--laptop-manifest-path", dest="laptop_manifest_path")
    coding_briefing.add_argument("--no-llm", action="store_true", dest="no_llm")
    coding_briefing.add_argument("--no-memory", action="store_false", dest="use_memory", default=True)
    coding_briefing.add_argument("--json", action="store_true", dest="json_output")

    coding_focus = sub.add_parser(
        "coding-focus-proposals",
        help="Build dry-run coding focus calendar proposals from Rocky's coding briefing.",
    )
    coding_focus.add_argument("--planning-date", dest="planning_date")
    coding_focus.add_argument("--laptop-manifest-path", dest="laptop_manifest_path")
    coding_focus.add_argument("--db-path", dest="db_path")
    coding_focus.add_argument("--ledger-path", dest="ledger_path")
    coding_focus.add_argument("--max-blocks", type=int, default=2, dest="max_blocks")
    coding_focus.add_argument("--no-memory", action="store_false", dest="use_memory", default=True)
    coding_focus.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    coding_focus.add_argument("--json", action="store_true", dest="json_output")

    coding_book = sub.add_parser(
        "coding-focus-book",
        help="Supervised live booking for one selected Rocky coding focus proposal.",
    )
    coding_book.add_argument("--idempotency-key", required=True, dest="idempotency_key")
    coding_book.add_argument("--planning-date", dest="planning_date")
    coding_book.add_argument("--calendar", default="Calendar", dest="calendar_name")
    coding_book.add_argument("--db-path", dest="db_path")
    coding_book.add_argument("--state-db", dest="state_db")
    coding_book.add_argument("--scheduler-db", dest="scheduler_db")
    coding_book.add_argument("--ledger-path", dest="ledger_path")
    coding_book.add_argument("--live", action="store_true")
    coding_book.add_argument("--json", action="store_true", dest="json_output")

    coding_scheduler = sub.add_parser(
        "coding-work-scheduler-run",
        help="Run Rocky's noon coding briefing and automatic focus booking scheduler.",
    )
    coding_scheduler.add_argument("--planning-date", dest="planning_date")
    coding_scheduler.add_argument("--live", action="store_true")
    coding_scheduler.add_argument("--notify", action="store_true")
    coding_scheduler.add_argument("--notification-dry-run", action="store_true", dest="notification_dry_run")
    coding_scheduler.add_argument("--notification-channel-id", dest="notification_channel_id")
    coding_scheduler.add_argument("--laptop-manifest-path", dest="laptop_manifest_path")
    coding_scheduler.add_argument("--max-blocks", type=int, default=2, dest="max_blocks")
    coding_scheduler.add_argument("--no-memory", action="store_false", dest="use_memory", default=True)
    coding_scheduler.add_argument("--db-path", dest="db_path")
    coding_scheduler.add_argument("--calendar-state-db", dest="calendar_state_db")
    coding_scheduler.add_argument("--scheduler-db", dest="scheduler_db")
    coding_scheduler.add_argument("--ledger-path", dest="ledger_path")
    coding_scheduler.add_argument("--state-file", default="/Users/clawdbot/.openclaw/state/coding_work_briefing_scheduler.json", dest="state_file")
    coding_scheduler.add_argument("--lock-ttl-seconds", type=int, default=1800, dest="lock_ttl_seconds")
    coding_scheduler.add_argument("--no-write-audit", action="store_false", dest="write_audit", default=True)
    coding_scheduler.add_argument("--json", action="store_true", dest="json_output")

    coding_llm = sub.add_parser(
        "coding-work-llm-health",
        help="Check Rocky's Codex LLM path used for coding work ranking.",
    )
    coding_llm.add_argument("--json", action="store_true", dest="json_output")

    notification_dispatch = sub.add_parser(
        "assistant-notification-dispatch",
        help="Dispatch or dry-run a safe assistant failure notification.",
    )
    notification_dispatch.add_argument("--status", required=True)
    notification_dispatch.add_argument("--reason", required=True)
    notification_dispatch.add_argument("--target-date", dest="target_date")
    notification_dispatch.add_argument("--idempotency-key", dest="idempotency_key")
    notification_dispatch.add_argument("--channel-id", default="1485710572325703901", dest="channel_id")
    notification_dispatch.add_argument("--config-path", default="/Users/clawdbot/.openclaw/openclaw.json", dest="config_path")
    notification_dispatch.add_argument("--ledger-path", dest="ledger_path")
    notification_dispatch.add_argument("--scheduler-db", dest="scheduler_db")
    notification_dispatch.add_argument("--dry-run", action="store_true", dest="dry_run")
    notification_dispatch.add_argument("--json", action="store_true", dest="json_output")

    agentmail_health = sub.add_parser(
        "agentmail-bridge-health",
        help="Check AgentMail bridge source/deploy drift and LaunchAgent status.",
    )
    agentmail_health.add_argument("--run-tests", action="store_true", help="Run tracked Node tests as part of health.")
    agentmail_health.add_argument("--no-launchctl", action="store_false", dest="read_launchctl", default=True)
    agentmail_health.add_argument("--json", action="store_true", dest="json_output")

    scheduler_health = sub.add_parser(
        "assistant-scheduler-health",
        help="Run read-only assistant scheduler health checks.",
    )
    scheduler_health.add_argument("--job", help="Optional scheduler job name to check.")
    scheduler_health.add_argument("--state-db", dest="state_db", help="Optional assistant scheduler SQLite path.")
    scheduler_health.add_argument("--audit-ledger", dest="audit_ledger", help="Optional assistant audit JSONL path.")
    scheduler_health.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    scheduler_health.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    dead_letters = sub.add_parser(
        "assistant-dead-letters",
        help="Show assistant scheduler dead-letter records.",
    )
    dead_letters.add_argument("--limit", type=int, default=20, help="Number of records to show.")
    dead_letters.add_argument(
        "--status",
        default="open",
        choices=["open", "acknowledged", "recovered", "ignored", "all"],
        help="Dead-letter status filter.",
    )
    dead_letters.add_argument("--state-db", dest="state_db", help="Optional assistant scheduler SQLite path.")
    dead_letters.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    lock_smoke = sub.add_parser(
        "assistant-lock-smoke",
        help="Smoke-test assistant scheduler duplicate-run locking.",
    )
    lock_smoke.add_argument("--workflow", required=True, help="Workflow name for the smoke lock.")
    lock_smoke.add_argument("--idempotency-key", required=True, dest="idempotency_key", help="Smoke idempotency key.")
    lock_smoke.add_argument("--ttl-seconds", type=int, default=60, dest="ttl_seconds", help="Lock TTL in seconds.")
    lock_smoke.add_argument("--state-db", dest="state_db", help="Optional assistant scheduler SQLite path.")
    lock_smoke.add_argument("--audit-ledger", dest="audit_ledger", help="Optional assistant audit JSONL path.")
    lock_smoke.add_argument(
        "--no-write-audit",
        action="store_false",
        dest="write_audit",
        default=True,
        help="Do not append assistant audit events.",
    )
    lock_smoke.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    runtime_version = sub.add_parser(
        "runtime-version",
        help="Report deployed Rocky runtime helper provenance.",
    )
    runtime_version.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    runtime_answer = sub.add_parser(
        "runtime-answer",
        help="Answer one prompt through Rocky's read-only runtime memory path.",
    )
    runtime_answer.add_argument("--prompt", required=True, help="Prompt/question to answer.")
    runtime_answer.add_argument(
        "--read-only",
        action="store_true",
        dest="read_only",
        help="Required guard: refuse to answer unless explicitly set.",
    )
    runtime_answer.add_argument(
        "--limit",
        type=int,
        default=RUNTIME_ANSWER_CONTEXT_LIMIT,
        help="Maximum Obsidian memory hits to use (default: 5).",
    )
    runtime_answer.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable answer.",
    )

    work_signal = sub.add_parser(
        "work-signal-summary",
        help="Collect sanitized Apple Mail/Calendar productivity signals through a read-only bridge.",
    )
    work_signal.add_argument(
        "--source",
        choices=["apple-mail", "apple-calendar", "all"],
        default="all",
        help="Signal source to summarize (default: all).",
    )
    work_signal.add_argument("--project", default="private-local-memory-os", help="Project slug for the summary.")
    work_signal.add_argument("--since", default="7d", help="Collection window, e.g. 7d or YYYY-MM-DD.")
    work_signal.add_argument("--limit", type=int, default=100, help="Maximum raw metadata records to inspect.")
    work_signal.add_argument(
        "--read-only",
        action="store_true",
        dest="read_only",
        help="Required guard: refuse to collect unless explicitly set.",
    )
    work_signal.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of Markdown summaries.",
    )

    lesson_capture = sub.add_parser(
        "lesson-capture",
        help="Append one structured improvement lesson.",
    )
    lesson_capture.add_argument("--workflow", required=True, help="Workflow or context where the lesson was discovered.")
    lesson_capture.add_argument("--symptom", required=True, help="Short summary of the failure or friction.")
    lesson_capture.add_argument("--root-cause", required=True, dest="root_cause", help="Likely root cause in one sentence.")
    lesson_capture.add_argument(
        "--safer-future-behavior",
        required=True,
        dest="safer_future_behavior",
        help="Short future rule or safer behavior.",
    )
    lesson_capture.add_argument(
        "--status",
        default="candidate",
        choices=["candidate", "validated", "rejected"],
        help="Lesson status (default: candidate).",
    )
    lesson_capture.add_argument(
        "--confidence",
        default="medium",
        choices=["low", "medium", "high"],
        help="Confidence in the lesson (default: medium).",
    )
    lesson_capture.add_argument("--source-ref", dest="source_ref", help="Optional file, command, or issue reference.")
    lesson_capture.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Optional tag. May be passed multiple times.",
    )
    lesson_capture.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    lesson_recent = sub.add_parser(
        "lesson-recent",
        help="Show recent structured improvement lessons.",
    )
    lesson_recent.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent lessons to show (default: 10).",
    )
    lesson_recent.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    memory_promote = sub.add_parser(
        "memory-promote",
        help="Promote one durable cross-agent memory candidate into Rocky's Obsidian vault.",
    )
    memory_promote.add_argument("--agent-name", required=True, dest="agent_name", help="Source agent name.")
    memory_promote.add_argument(
        "--note-type",
        required=True,
        dest="note_type",
        choices=["project", "company", "person", "theme", "weekly", "deal", "meeting", "decision", "okr", "weekly-report-cz", "area-moc", "inbox-note"],
        help="Durable memory note category.",
    )
    memory_promote.add_argument("--title", required=True, help="Note title.")
    memory_promote.add_argument("--summary", required=True, help="Compact durable summary.")
    memory_promote.add_argument("--body", help="Optional additional durable context.")
    memory_promote.add_argument("--source-ref", dest="source_ref", help="Optional source artifact path or identifier.")
    memory_promote.add_argument("--source-date", dest="source_date", help="Optional source date.")
    memory_promote.add_argument("--dedupe-key", dest="dedupe_key", help="Optional stable dedupe key.")
    memory_promote.add_argument("--tag", action="append", default=[], help="Optional tag. May be passed multiple times.")
    memory_promote.add_argument(
        "--related-entity",
        action="append",
        default=[],
        dest="related_entities",
        help="Optional related entity. May be passed multiple times.",
    )
    memory_promote.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    recent_promotions = sub.add_parser(
        "memory-recent-promotions",
        help="Show recent durable-memory promotion events.",
    )
    recent_promotions.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent promotion events to show (default: 10).",
    )
    recent_promotions.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    obsidian_status = sub.add_parser(
        "obsidian-status",
        help="Show Layer 3 Obsidian vault/QMD status.",
    )
    obsidian_status.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    obsidian_sync = sub.add_parser(
        "obsidian-sync",
        help="Ensure the Obsidian vault is configured and indexed in QMD.",
    )
    obsidian_sync.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    obsidian_query = sub.add_parser(
        "obsidian-query",
        help="Search Obsidian Layer 3 notes through QMD.",
    )
    obsidian_query.add_argument("query", help="Topic/query to retrieve from the vault.")
    obsidian_query.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of note hits to include (default: 5).",
    )
    obsidian_query.add_argument(
        "--mode",
        choices=["query", "search"],
        default="query",
        help="QMD retrieval mode to use (default: query).",
    )
    obsidian_query.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    obsidian_write = sub.add_parser(
        "obsidian-write",
        help="Safely create or append a note in the dedicated Obsidian memory folder.",
    )
    obsidian_write.add_argument("title", help="Note title.")
    obsidian_write.add_argument("--content", help="Inline markdown content for the note.")
    obsidian_write.add_argument(
        "--content-file",
        help="Path to a file containing markdown content.",
    )
    obsidian_write.add_argument(
        "--append",
        action="store_true",
        help="Append to the note if it already exists.",
    )
    obsidian_write.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output raw JSON instead of a human-readable summary.",
    )

    return parser


def cmd_health_report(args) -> int:
    statuses = run_all_checks()
    print(format_health_report(statuses))
    blocked = sum(1 for s in statuses if s.status == "blocked")
    return 1 if blocked > 0 else 0


def cmd_capability_report(args) -> int:
    statuses = run_all_checks()
    snapshot = get_capability_snapshot(health_statuses=statuses)
    print(format_capability_report(snapshot))
    blocked = sum(1 for c in snapshot if c.status == "blocked")
    return 1 if blocked > 0 else 0


def cmd_response_check_fixtures(args) -> int:
    fixture_path = args.fixture_path
    if not Path(fixture_path).exists():
        print(f"[ERROR] Fixture file not found: {fixture_path}", file=sys.stderr)
        return 1

    results = check_response_from_fixture(fixture_path)

    if args.json_output:
        print(json.dumps(results, indent=2))
        return 0

    bad_keys = [k for k in results if k.endswith("_bad")]
    good_keys = [k for k in results if k.endswith("_good")]
    violations_in_bad = sum(results[k]["violations"] for k in bad_keys)
    violations_in_good = sum(results[k]["violations"] for k in good_keys)

    print("Response Fixture Check")
    print("=" * 30)
    print(f"Bad responses checked:  {len(bad_keys)}  — violations found: {violations_in_bad}")
    print(f"Good responses checked: {len(good_keys)} — violations found: {violations_in_good}")

    if violations_in_good > 0:
        print(f"[WARN] {violations_in_good} violation(s) flagged in good responses.")
        for k in good_keys:
            if results[k]["violations"] > 0:
                for detail in results[k]["details"]:
                    print(f"       {k}: {detail}")
        return 1

    if violations_in_bad == 0 and bad_keys:
        print("[WARN] No violations detected in bad responses — checker may need calibration.")
        return 1

    print("[OK] Checker is catching bad responses and passing good ones.")
    return 0


def cmd_show_recent_runs(args) -> int:
    ledger = RunLedger()
    runs = ledger.get_recent_runs(limit=args.limit)

    if not runs:
        print("No runs recorded yet.")
        return 0

    if args.json_output:
        print(json.dumps([asdict(r) for r in runs], indent=2))
        return 0

    exec_icons = {
        "completed": "[OK]",
        "failed": "[FAIL]",
        "timed_out": "[TIMEOUT]",
        "partial": "[PART]",
        "started": "[...]",
    }
    deliv_labels = {
        "delivered": " -> delivered",
        "blocked_auth": " -> blocked_auth",
        "blocked_permissions": " -> blocked_perms",
        "blocked_rate_limit": " -> blocked_rate",
        "failed": " -> delivery_failed",
        "timed_out": " -> delivery_timeout",
    }

    print(f"Recent Runs (latest {len(runs)})")
    print("=" * 60)
    for r in runs:
        exec_sym = exec_icons.get(r.execution_status, "[?]")
        deliv_sym = deliv_labels.get(r.delivery_status, f" -> {r.delivery_status}") if r.delivery_status else ""
        ts = r.timestamp_started[:19] if r.timestamp_started else "?"
        print(f"{exec_sym}{deliv_sym}  {r.run_id}  {r.workflow_name}  [{ts}]")
        if r.human_summary:
            print(f"     {r.human_summary}")

    return 0


def cmd_show_bob_jobs(args) -> int:
    ledger = BobJobLedger()
    records = ledger.recent(limit=args.limit)
    if args.json_output:
        print(json.dumps([asdict(record) for record in records], indent=2))
    else:
        print(format_bob_jobs(records))
    return 0


def cmd_show_bob_reports(args) -> int:
    ledger = BobReportLedger()
    records = ledger.recent(limit=args.limit)
    if args.json_output:
        print(json.dumps([asdict(record) for record in records], indent=2, ensure_ascii=False))
    else:
        print(format_bob_reports(records))
    return 0


def cmd_report_bob_stage(args) -> int:
    ledger = BobJobLedger()
    record = ledger.get(args.job_id)
    if record is None:
        print(f"[ERROR] Bob job {args.job_id} not found", file=sys.stderr)
        return 1
    report = report_bob_stage(
        record,
        args.stage or derive_stage_from_job(record),
        summary=args.summary,
        plan_path=args.plan_path,
        artifact_paths=args.artifact_path,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json_output:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    else:
        print(f"Bob report {report.status}: {report.job_id} {report.stage} -> {report.channel_id}")
        if report.error:
            print(f"     {report.error}")
    return 0 if report.status in {"posted", "dry_run", "skipped_duplicate"} else 1


def cmd_mark_bob_dusan_notified(args) -> int:
    ledger = BobJobLedger()
    try:
        record = ledger.mark_dusan_notified(args.job_id, note=args.note)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(asdict(record), indent=2))
    else:
        print(f"Marked Dusan notification confirmed for {record.job_id}.")
    return 0


def cmd_show_subagent_failures(args) -> int:
    ledger = SubagentFailureLedger()
    events = list(ledger.latest_events().values())
    events.sort(key=lambda item: item.timestamp, reverse=True)
    events = events[: args.limit]

    if args.json_output:
        print(json.dumps([asdict(event) for event in events], indent=2))
        return 0

    if not events:
        print("No subagent failure events recorded yet.")
        return 0

    print(f"Subagent Failures (latest {len(events)})")
    print("=" * 60)
    for event in events:
        print(
            f"[{event.recovery_status}] {event.event_id} "
            f"{event.agent}/{event.job_name} status={event.status} "
            f"validation={validation_level(event)}"
        )
        if event.human_summary:
            print(f"     {event.human_summary}")
        elif event.error_message:
            print(f"     {event.error_message[:160]}")
        print(f"     {render_validation_result(event)}")
    return 0


def cmd_recover_subagent_failures(args) -> int:
    result = recover_pending_failures(limit=args.limit)
    if args.json_output:
        print(json.dumps(result, indent=2))
        return 0
    print(f"Processed {result['count']} subagent failure event(s).")
    for event in result["processed"]:
        print(
            f"- {event['event_id']} {event['agent']}/{event['job_name']}: "
            f"{event['recovery_status']} "
            f"(validation={event.get('validation_result', {}).get('level', 'not_verified')})"
        )
    return 0


def _parse_isoish(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _display_date(source: dict[str, Any]) -> str:
    for key in ("published_at", "updated_at", "created_at"):
        parsed = _parse_isoish(source.get(key))
        if parsed is not None:
            return parsed.date().isoformat()
    return "unknown-date"


def _source_sort_key(item: dict[str, Any]) -> tuple[float, float, int]:
    source = item["source"]
    parsed = None
    for key in ("published_at", "updated_at", "created_at"):
        parsed = _parse_isoish(source.get(key))
        if parsed is not None:
            break
    ts = parsed.timestamp() if parsed is not None else 0.0
    score = float(item["search_hit"].get("score") or 0.0)
    source_id = int(source.get("id") or 0)
    return (-ts, score, -source_id)


def _compact_runtime_text(value: Any, *, max_length: int = RUNTIME_ANSWER_MAX_SOURCE_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "..."


def _runtime_prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _git_commit() -> str:
    return _git_value("rev-parse", "--short", "HEAD")


def _git_dirty() -> bool:
    return bool(_git_value("status", "--porcelain"))


def _load_runtime_manifest() -> dict[str, Any]:
    if not RUNTIME_DEPLOY_MANIFEST_PATH.is_file():
        return {}
    try:
        data = json.loads(RUNTIME_DEPLOY_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def runtime_version_payload() -> dict[str, Any]:
    manifest = _load_runtime_manifest()
    local_hashes = {rel: _sha256_file(ROOT / rel) for rel in RUNTIME_DEPLOYABLE_FILES}
    manifest_commit = str(manifest.get("source_commit") or "")
    source_commit = manifest_commit or _git_commit() or "unknown"
    source_dirty = bool(manifest.get("source_dirty", False)) if manifest_commit else _git_dirty()
    if not manifest_commit and source_dirty:
        source_commit = f"{source_commit}-dirty" if source_commit != "unknown" else "unknown-dirty"
    file_hashes = manifest.get("file_hashes") if isinstance(manifest.get("file_hashes"), dict) else {}
    merged_hashes = {**{str(key): str(value) for key, value in dict(file_hashes).items()}, **local_hashes}
    return {
        "status": "ok",
        "tool": ROCKY_RUNTIME_TOOL_NAME,
        "version": ROCKY_RUNTIME_TOOL_VERSION,
        "source_repo": str(manifest.get("source_repo") or ROCKY_RUNTIME_SOURCE_REPO),
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "deployed_at": str(manifest.get("deployed_at", "")),
        "deployed_by": str(manifest.get("deployed_by", "")),
        "file_hashes": merged_hashes,
        "manifest_hash": _sha256_file(RUNTIME_DEPLOY_MANIFEST_PATH),
        "manifest_path": str(RUNTIME_DEPLOY_MANIFEST_PATH),
        "runtime_target": str(manifest.get("runtime_target") or ROCKY_RUNTIME_TARGET),
        "writes_attempted": [],
        "write_guard": "not_applicable_read_only_status",
    }


def format_runtime_version(payload: dict[str, Any]) -> str:
    rows = [
        ("tool", payload.get("tool", "")),
        ("version", payload.get("version", "")),
        ("source_repo", payload.get("source_repo", "")),
        ("source_commit", payload.get("source_commit", "")),
        ("source_dirty", payload.get("source_dirty", "")),
        ("deployed_at", payload.get("deployed_at", "")),
        ("deployed_by", payload.get("deployed_by", "")),
        ("runtime_target", payload.get("runtime_target", "")),
        ("manifest_hash", payload.get("manifest_hash", "")),
    ]
    lines = ["Rocky runtime tools version"]
    lines.extend(f"- {key}: {value}" for key, value in rows)
    lines.append("- file_hashes:")
    for rel, digest in sorted(dict(payload.get("file_hashes", {})).items()):
        lines.append(f"  - {rel}: {digest}")
    return "\n".join(lines) + "\n"


def _runtime_blocked_payload(prompt: str, reason: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "read_only": False,
        "answer": "",
        "prompt_hash": _runtime_prompt_hash(prompt),
        "model": "",
        "memory_sources": [],
        "source_counts": {"obsidian": 0, "student_archive": 0},
        "writes_attempted": [],
        "write_guard": "enforced",
        "errors": [reason],
    }


def _runtime_answer_fts_query(prompt: str) -> str:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", prompt):
        clean = term.replace('"', "").strip("_-")
        if not clean:
            continue
        if clean.lower() in {"what", "should", "about", "with", "from", "that", "this", "rocky", "remember"}:
            continue
        if clean.lower() not in {item.lower() for item in terms}:
            terms.append(clean)
        if len(terms) >= 8:
            break
    return " OR ".join(f'"{term}"' for term in terms)


def _student_archive_memory_sources(prompt: str, *, per_archive_limit: int = 2) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    sources: list[dict[str, Any]] = []
    fts_query = _runtime_answer_fts_query(prompt)
    if not fts_query:
        return sources, errors
    try:
        from student.config import load_config
    except Exception as exc:
        return sources, [f"student archive unavailable: {exc}"]
    try:
        config = load_config()
    except Exception as exc:
        return sources, [f"student config unavailable: {exc}"]
    sql = """
        WITH combined AS (
            SELECT
                s.id AS source_id,
                s.source_type AS source_type,
                COALESCE(s.title, '') AS title,
                COALESCE(s.summary, '') AS summary,
                s.ingestion_status AS ingestion_status,
                s.ingestion_quality AS ingestion_quality,
                bm25(sources_fts) AS score
            FROM sources_fts
            JOIN sources s ON s.id = sources_fts.rowid
            WHERE sources_fts MATCH ?

            UNION ALL

            SELECT
                s.id AS source_id,
                s.source_type AS source_type,
                COALESCE(s.title, '') AS title,
                COALESCE(i.insight_text, '') AS summary,
                s.ingestion_status AS ingestion_status,
                s.ingestion_quality AS ingestion_quality,
                bm25(insights_fts) + 0.5 AS score
            FROM insights_fts
            JOIN insights i ON i.id = insights_fts.rowid
            JOIN sources s ON s.id = i.source_id
            WHERE insights_fts MATCH ?
        )
        SELECT
            source_id,
            source_type,
            title,
            summary,
            ingestion_status,
            ingestion_quality,
            MIN(score) AS score
        FROM combined
        GROUP BY source_id, source_type, title, summary, ingestion_status, ingestion_quality
        ORDER BY score
        LIMIT ?
    """
    for archive_id in RUNTIME_ANSWER_ARCHIVES:
        archive = config.archives.get(archive_id)
        if archive is None or not archive.sqlite_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{archive.sqlite_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(sql, (fts_query, fts_query, per_archive_limit)).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"student archive {archive_id} query failed: {exc}")
            continue
        for row in rows:
            summary = _compact_runtime_text(row["summary"])
            sources.append(
                {
                    "source": "student-archive",
                    "archive": archive_id,
                    "source_id": int(row["source_id"]),
                    "source_type": row["source_type"],
                    "title": _compact_runtime_text(row["title"], max_length=120),
                    "summary": summary,
                    "score": row["score"],
                    "status": row["ingestion_status"],
                    "quality": row["ingestion_quality"],
                }
            )
    return sources, errors


def _obsidian_runtime_memory_sources(prompt: str, *, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_paths: set[str] = set()
    for mode in ("search", "query"):
        try:
            payload = query_obsidian_vault(prompt, limit=limit, mode=mode)
        except Exception as exc:
            errors.append(f"obsidian {mode} failed: {exc}")
            continue
        if payload.get("status") != "ok":
            reason = payload.get("reason") or payload.get("error") or payload.get("status")
            errors.append(f"obsidian {mode} unavailable: {reason}")
            continue
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            path = _compact_runtime_text(item.get("path"), max_length=240)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            sources.append(
                {
                    "source": "obsidian-layer3",
                    "title": _compact_runtime_text(item.get("title"), max_length=120),
                    "path": path,
                    "score": item.get("score"),
                    "snippet": _compact_runtime_text(item.get("snippet")),
                }
            )
            if len(sources) >= limit:
                return sources, errors
    return sources, errors


def _runtime_context_lines(memory_sources: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for index, source in enumerate(memory_sources, start=1):
        if source.get("source") == "obsidian-layer3":
            lines.append(
                f"{index}. [obsidian-layer3] {source.get('title') or '(untitled)'} | path={source.get('path') or 'unknown'} | summary={source.get('snippet') or 'No safe snippet.'}"
            )
        elif source.get("source") == "student-archive":
            lines.append(
                f"{index}. [student-archive:{source.get('archive')}] #{source.get('source_id')} {source.get('title') or '(untitled)'} | summary={source.get('summary') or 'No safe summary.'}"
            )
    return lines


def _build_runtime_llm_prompt(prompt: str, memory_sources: list[dict[str, Any]]) -> str:
    context = "\n".join(_runtime_context_lines(memory_sources))
    context = context[:RUNTIME_ANSWER_MAX_CONTEXT_CHARS]
    if not context:
        context = "No matching read-only memory context was retrieved."
    return (
        "You are Rocky answering through a read-only runtime evaluation path.\n"
        "Use only the retrieved memory context below plus explicit uncertainty.\n"
        "Cite safe source metadata such as note title, path, archive, or source id when useful.\n"
        "If the question involves production, runtime, deployment, environment, or current state, say that Rocky must verify live state before acting.\n"
        "Do not write memory, promote memory, ingest artifacts, post messages, mutate files, or claim that memory is always current.\n"
        "Keep the answer concise and operational.\n\n"
        f"User prompt:\n{prompt}\n\n"
        f"Retrieved read-only memory context:\n{context}\n"
    )


def _generate_runtime_answer_text(llm_prompt: str) -> tuple[str | None, str]:
    from student.config import load_config
    from student.llm import _run_with_timeout, generate_text_response

    config = load_config()
    model = str(
        getattr(config, "validation_ingestion_model", "")
        or getattr(config, "openai_codex_model", "")
        or ""
    )
    try:
        answer = _run_with_timeout(
            generate_text_response,
            llm_prompt,
            config,
            model_override=model,
            reasoning_override="low",
            timeout_seconds=RUNTIME_ANSWER_LLM_TIMEOUT_SECONDS,
        )
    except Exception:
        answer = None
    return answer, model


def _runtime_source_grounded_answer(prompt: str, memory_sources: list[dict[str, Any]]) -> str:
    lines = [
        f"Rocky's read-only memory context for this prompt says: {prompt}",
        "Relevant memory sources:",
    ]
    for source in memory_sources[:4]:
        if source.get("source") == "obsidian-layer3":
            title = source.get("title") or "Untitled Obsidian note"
            path = source.get("path") or "unknown path"
            snippet = source.get("snippet") or "No safe snippet available."
            lines.append(f"- {title} ({path}): {snippet}")
        elif source.get("source") == "student-archive":
            title = source.get("title") or "Untitled archive source"
            archive = source.get("archive") or "unknown"
            source_id = source.get("source_id") or "unknown"
            summary = source.get("summary") or "No safe summary available."
            lines.append(f"- {title} (student archive {archive} source {source_id}): {summary}")
    lines.append(
        "Before acting on production, runtime, deployment, environment, or other current-state facts, Rocky must verify live state and the current source of truth."
    )
    lines.append("Propagation or memory changes require explicit review; Codex-local-only memory stays local unless explicitly reclassified for Rocky.")
    lines.append("This was a read-only answer path; it did not write, promote, ingest, or mutate memory.")
    return "\n".join(lines)


def build_runtime_answer(prompt: str, *, read_only: bool, limit: int = RUNTIME_ANSWER_CONTEXT_LIMIT) -> dict[str, Any]:
    prompt = str(prompt or "").strip()
    if not read_only:
        return _runtime_blocked_payload(prompt, "runtime-answer requires --read-only")
    if not prompt:
        return _runtime_blocked_payload(prompt, "runtime-answer requires a non-empty prompt")

    errors: list[str] = []
    obsidian_sources, obsidian_errors = _obsidian_runtime_memory_sources(prompt, limit=max(1, limit))
    archive_sources, archive_errors = ([], [])
    if len(obsidian_sources) < 2:
        archive_sources, archive_errors = _student_archive_memory_sources(prompt)
    errors.extend(obsidian_errors)
    errors.extend(archive_errors)
    memory_sources = [*obsidian_sources, *archive_sources]
    source_counts = {
        "obsidian": len(obsidian_sources),
        "student_archive": len(archive_sources),
    }

    if not memory_sources:
        return {
            "status": "ok",
            "read_only": True,
            "answer": (
                "I did not find matching read-only Rocky memory context for this prompt. "
                "Treat this as limited coverage and verify live/runtime state before acting. "
                "Propagation or memory changes require explicit review; Codex-local-only memory stays local unless explicitly reclassified for Rocky."
            ),
            "prompt_hash": _runtime_prompt_hash(prompt),
            "model": "",
            "memory_sources": [],
            "source_counts": source_counts,
            "writes_attempted": [],
            "write_guard": "enforced",
            "errors": errors,
        }

    llm_prompt = _build_runtime_llm_prompt(prompt, memory_sources)
    try:
        answer, model = _generate_runtime_answer_text(llm_prompt)
    except Exception as exc:
        return {
            "status": "failed",
            "read_only": True,
            "answer": "",
            "prompt_hash": _runtime_prompt_hash(prompt),
            "model": "",
            "memory_sources": memory_sources,
            "source_counts": source_counts,
            "writes_attempted": [],
            "write_guard": "enforced",
            "errors": [*errors, f"runtime answer generation failed: {exc}"],
        }
    if not answer:
        answer = _runtime_source_grounded_answer(prompt, memory_sources)
        errors = [*errors, "runtime answer generation returned no text; used source-grounded fallback"]
    return {
        "status": "ok",
        "read_only": True,
        "answer": answer.strip(),
        "prompt_hash": _runtime_prompt_hash(prompt),
        "model": model,
        "memory_sources": memory_sources,
        "source_counts": source_counts,
        "writes_attempted": [],
        "write_guard": "enforced",
        "errors": errors,
    }


def _normalize_work_signal_sources(source: str) -> list[str]:
    key = str(source or "all").strip().lower().replace("_", "-")
    if key == "all":
        return ["apple_mail", "apple_calendar"]
    if key not in WORK_SIGNAL_SOURCES:
        raise ValueError(f"unsupported work signal source: {source}")
    return [WORK_SIGNAL_SOURCES[key]]


def _work_signal_period(since: str) -> tuple[str, str, int]:
    now = datetime.now(ZoneInfo(WORK_SIGNAL_TIMEZONE))
    since = str(since or "7d").strip().lower()
    match = re.fullmatch(r"(\d+)d", since)
    if match:
        days = max(1, int(match.group(1)))
        start = now - timedelta(days=days)
        return start.date().isoformat(), now.date().isoformat(), days
    try:
        start = datetime.fromisoformat(since.replace("z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(WORK_SIGNAL_TIMEZONE))
        days = max(1, (now.date() - start.astimezone(ZoneInfo(WORK_SIGNAL_TIMEZONE)).date()).days)
        return start.astimezone(ZoneInfo(WORK_SIGNAL_TIMEZONE)).date().isoformat(), now.date().isoformat(), days
    except ValueError as exc:
        raise ValueError("--since must be like 7d or YYYY-MM-DD") from exc


def _invoke_work_signal_betty_helper(days: int, limit: int) -> dict[str, Any]:
    if not BETTY_PYTHON.exists():
        raise RuntimeError(f"Betty python missing at {BETTY_PYTHON}")
    if not BETTY_WEEKLY_REPORT_HELPER.exists():
        raise RuntimeError(f"Betty weekly report helper missing at {BETTY_WEEKLY_REPORT_HELPER}")
    cmd = [
        str(BETTY_PYTHON),
        str(BETTY_WEEKLY_REPORT_HELPER),
        "collect",
        "--days",
        str(max(days, 1)),
        "--limit",
        str(max(limit, 1)),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"Betty helper failed: {result.stderr.strip()[:240]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Betty helper output was not JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _work_signal_domain_hints(record: dict[str, Any]) -> set[str]:
    raw_parts: list[str] = []
    for key in ("subject", "title", "summary", "calendar", "source", "area"):
        value = record.get(key)
        if isinstance(value, str):
            raw_parts.append(value)
    for key in ("internal_participants", "external_participants", "attendees"):
        value = record.get(key)
        if isinstance(value, list):
            raw_parts.extend(str(item) for item in value)
    text = " ".join(raw_parts).lower()
    hints: set[str] = set()
    if any(term in text for term in ("deal intelligence", "specter", "pitch deck", "railway", "startup-ranker")):
        hints.add("deal-intelligence")
    if any(term in text for term in ("matchbook", "matchmaking", "vc_startup_platform", "supabase")):
        hints.add("matchbook-matchmaking")
    if any(term in text for term in ("feedback hub", "feedback app", "feedback_app")):
        hints.add("feedback-hub")
    if any(term in text for term in ("openclaw", "hermes", "rocky", "clawdbot")):
        hints.add("openclaw-hermes-rocky")
    if any(term in text for term in ("codex", "private-local-memory-os", "memory os")):
        hints.add("private-local-memory-os")
    if any(term in text for term in ("codex workflow", "approved memory", "proposed memory")):
        hints.add("codex-workflow")
    return hints


def _work_signal_source_ref(source: str, project: str, period_start: str, period_end: str, evidence_hash: str) -> str:
    source_slug = source.replace("_", "-")
    return f"rocky-bridge://{source_slug}/{hashlib.sha256(f'{project}:{period_start}:{period_end}:{evidence_hash}'.encode()).hexdigest()[:16]}"


def _work_signal_safe_error(exc: Exception) -> str:
    text = str(exc).splitlines()[0][:180]
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", text)
    text = re.sub(r"(?:sk-|ntn_|xoxb-|ghp_|Bearer\s+)[A-Za-z0-9_\-]{8,}", "[redacted-secret]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)\b(subject|from|to|cc|bcc|attendee|description|invite body|snippet):\s*.*", r"\1: [redacted]", text)
    return text or exc.__class__.__name__


def _build_work_signal_source_summary(
    *,
    source: str,
    project: str,
    period_start: str,
    period_end: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    domain_counts: dict[str, int] = {}
    for record in records:
        for domain in _work_signal_domain_hints(record):
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
    domain_hints = sorted(domain_counts) or [project]
    evidence_basis = {
        "source": source,
        "project": project,
        "period_start": period_start,
        "period_end": period_end,
        "activity_count": len(records),
        "domain_counts": domain_counts,
    }
    evidence_hash = hashlib.sha256(json.dumps(evidence_basis, sort_keys=True).encode()).hexdigest()[:16]
    source_label = "Apple Mail" if source == "apple_mail" else "Apple Calendar"
    domain_text = ", ".join(f"{domain}:{count}" for domain, count in sorted(domain_counts.items())) or "no domain-specific hints"
    return {
        "source": source,
        "adapter": "rocky_bridge",
        "domain_hints": domain_hints,
        "activity_count": len(records),
        "friction_rating": "",
        "estimated_minutes_saved": 0,
        "avoided_mistake": "unknown",
        "focus_interruption_count": "",
        "verification_completed": "unknown",
        "safe_tags": ["rocky-bridge", "metadata-only"],
        "source_refs": [_work_signal_source_ref(source, project, period_start, period_end, evidence_hash)],
        "redaction_status": "reviewed_metadata_only",
        "sensitivity_check": "Metadata-only Rocky bridge output; raw Mail and Calendar fields excluded.",
        "safe_summary": (
            f"{source_label} metadata reviewed {len(records)} record(s) for {project} "
            f"from {period_start} through {period_end}; raw private fields were excluded."
        ),
        "sanitized_evidence": (
            f"- reviewed_metadata_records: {len(records)}\n"
            f"- domain_hint_counts: {domain_text}\n"
            "- redaction: raw fields excluded before bridge output"
        ),
        "evidence_hash": evidence_hash,
    }


def _work_signal_blocked_payload(project: str, source: str, since: str, reason: str) -> dict[str, Any]:
    try:
        period_start, period_end, _days = _work_signal_period(since)
    except ValueError:
        now = datetime.now(ZoneInfo(WORK_SIGNAL_TIMEZONE)).date().isoformat()
        period_start = ""
        period_end = now
    return {
        "status": "blocked",
        "read_only": False,
        "project": project,
        "period_start": period_start,
        "period_end": period_end,
        "timezone": WORK_SIGNAL_TIMEZONE,
        "summaries": [],
        "source_counts": {"apple_mail_reviewed": 0, "apple_calendar_reviewed": 0},
        "raw_fields_excluded": WORK_SIGNAL_RAW_FIELDS_EXCLUDED,
        "writes_attempted": [],
        "errors": [reason],
    }


def build_work_signal_summary(
    *,
    source: str = "all",
    project: str = "private-local-memory-os",
    since: str = "7d",
    read_only: bool,
    limit: int = 100,
) -> dict[str, Any]:
    if not read_only:
        return _work_signal_blocked_payload(project, source, since, "work-signal-summary requires --read-only")
    try:
        sources = _normalize_work_signal_sources(source)
        period_start, period_end, days = _work_signal_period(since)
    except ValueError as exc:
        return _work_signal_blocked_payload(project, source, since, str(exc))

    errors: list[str] = []
    try:
        payload = _invoke_work_signal_betty_helper(days, limit)
    except Exception as exc:
        return {
            "status": "blocked",
            "read_only": True,
            "project": project,
            "period_start": period_start,
            "period_end": period_end,
            "timezone": WORK_SIGNAL_TIMEZONE,
            "summaries": [],
            "source_counts": {"apple_mail_reviewed": 0, "apple_calendar_reviewed": 0},
            "raw_fields_excluded": WORK_SIGNAL_RAW_FIELDS_EXCLUDED,
            "writes_attempted": [],
            "errors": [f"Rocky bridge source collection unavailable: {_work_signal_safe_error(exc)}"],
        }

    mail_records = [item for item in payload.get("email_threads") or [] if isinstance(item, dict)]
    calendar_records = [item for item in payload.get("meetings") or [] if isinstance(item, dict)]
    records_by_source = {"apple_mail": mail_records, "apple_calendar": calendar_records}
    summaries = [
        _build_work_signal_source_summary(
            source=item,
            project=project,
            period_start=period_start,
            period_end=period_end,
            records=records_by_source[item],
        )
        for item in sources
    ]
    source_counts = {
        "apple_mail_reviewed": len(mail_records) if "apple_mail" in sources else 0,
        "apple_calendar_reviewed": len(calendar_records) if "apple_calendar" in sources else 0,
    }
    return {
        "status": "partial" if errors else "ok",
        "read_only": True,
        "project": project,
        "period_start": period_start,
        "period_end": period_end,
        "timezone": WORK_SIGNAL_TIMEZONE,
        "summaries": summaries,
        "source_counts": source_counts,
        "raw_fields_excluded": WORK_SIGNAL_RAW_FIELDS_EXCLUDED,
        "writes_attempted": [],
        "errors": errors,
    }


def format_work_signal_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"Rocky work-signal summary: {payload.get('status')}",
        f"- project: {payload.get('project')}",
        f"- period: {payload.get('period_start')} -> {payload.get('period_end')} ({payload.get('timezone')})",
        "- writes_attempted: none",
    ]
    for summary in payload.get("summaries") or []:
        lines.extend(
            [
                "",
                f"## {str(summary.get('source', '')).replace('_', ' ').title()}",
                f"- activity_count: {summary.get('activity_count', 0)}",
                f"- domain_hints: {', '.join(summary.get('domain_hints') or [])}",
                str(summary.get("safe_summary") or ""),
            ]
        )
    errors = payload.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in errors)
    return "\n".join(lines) + "\n"


def build_archive_summary(
    query: str,
    *,
    archive_id: str = "knowladge",
    limit: int = 5,
    service: "StudentService | None" = None,
) -> dict[str, Any]:
    if service is None:
        from student.service import StudentService

        svc = StudentService()
    else:
        svc = service
    candidate_limit = max(limit * 3, 8)
    hits = svc.search_research(query, limit=candidate_limit, archive_id=archive_id)
    if not hits:
        return {
            "status": "ok",
            "archive": archive_id,
            "query": query,
            "summary": f"No recent archived items matched '{query}' in {archive_id}.",
            "coverage": "No matching sources found in the selected archive.",
            "items": [],
            "caveats": ["No result; coverage limited to the selected archive."],
        }

    enriched: list[dict[str, Any]] = []
    for hit in hits:
        source = svc.get_source(int(hit["source_id"]), archive_id=archive_id)
        if not source:
            continue
        enriched.append({"search_hit": hit, "source": source})

    enriched.sort(key=_source_sort_key)
    top_items = enriched[:limit]
    sources = [item["source"] for item in top_items]
    topic_summary = svc.summarize_topic(query, archive_id=archive_id)

    items = []
    for item in top_items:
        source = item["source"]
        hit = item["search_hit"]
        items.append(
            {
                "source_id": int(source["id"]),
                "title": source.get("title") or "(untitled)",
                "source_type": source.get("source_type") or "unknown",
                "date": _display_date(source),
                "archive": archive_id,
                "summary": source.get("summary") or hit.get("summary") or "",
                "score": hit.get("score"),
            }
        )

    coverage = f"{len(items)} source(s) shown from {archive_id}; ranked from recent search matches."
    caveats = list(topic_summary.get("caveats") or [])
    if not caveats:
        caveats = ["Coverage is limited to stored archive items that matched the query."]

    return {
        "status": "ok",
        "archive": archive_id,
        "query": query,
        "summary": topic_summary.get("summary") or f"Recent archived items for '{query}'.",
        "key_points": list(topic_summary.get("key_points") or []),
        "coverage": coverage,
        "items": items,
        "caveats": caveats,
    }


def format_archive_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"Archive summary for '{payload['query']}' ({payload['archive']}):",
        payload["summary"],
        f"Coverage: {payload['coverage']}",
    ]
    key_points = payload.get("key_points") or []
    if key_points:
        lines.append("Key points:")
        for point in key_points[:3]:
            lines.append(f"- {point}")

    items = payload.get("items") or []
    if items:
        lines.append("Sources:")
        for item in items:
            lines.append(
                f"- [{item['date']}] ({item['source_type']}) #{item['source_id']} {item['title']} — {item['summary']}"
            )

    caveats = payload.get("caveats") or []
    if caveats:
        lines.append("Caveats:")
        for caveat in caveats[:2]:
            lines.append(f"- {caveat}")
    return "\n".join(lines)


def cmd_archive_summary(args) -> int:
    payload = build_archive_summary(
        args.query,
        archive_id=args.archive,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_archive_summary(payload))
    return 0


def cmd_assistant_audit_recent(args) -> int:
    ledger = AssistantAuditLog(args.ledger_path)
    events = ledger.recent(limit=args.limit)
    if args.json_output:
        print(json.dumps([asdict(event) for event in events], indent=2, ensure_ascii=False))
        return 0
    if not events:
        print("No assistant audit events recorded yet.")
        return 0
    print(f"Assistant Audit Events (latest {len(events)})")
    print("=" * 60)
    for event in events:
        print(
            f"[{event.decision}] {event.created_at[:19]} "
            f"{event.event_type} {event.workflow} {event.audit_id}"
        )
        print(f"     {event.reason}")
    return 0


def cmd_calendar_policy_check(args) -> int:
    decision = evaluate_calendar_policy(
        kind=args.kind,
        day=args.date,
        start=args.start,
        duration_minutes=args.duration_minutes,
        source_refs=args.source_ref,
        label=args.label,
    )
    audit_log = AssistantAuditLog(args.ledger_path)
    event = audit_log.record_event(
        event_type="policy.allowed" if decision.allowed else "policy.violation",
        workflow="calendar_policy_check",
        idempotency_key=decision.idempotency_key,
        policy_version=POLICY_VERSION,
        decision="allowed" if decision.allowed else "blocked",
        reason=",".join(decision.reasons),
        sources=args.source_ref,
        artifacts={"policy_decision": decision.to_dict()},
    )
    payload = {
        "status": "allowed" if decision.allowed else "blocked",
        "audit_id": event.audit_id,
        "calendar_write_attempted": False,
        "policy_decision": decision.to_dict(),
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar policy {payload['status']}: {', '.join(decision.reasons)}")
        print(f"Audit ID: {event.audit_id}")
    return 0 if decision.allowed else 1


def cmd_calendar_dry_run(args) -> int:
    payload = build_calendar_dry_run(
        kind=args.kind,
        day=args.date,
        window_start=args.window_start,
        window_end=args.window_end,
        duration_minutes=args.duration_minutes,
        label=args.label,
        reason=args.reason,
        confidence=args.confidence,
        source_refs=args.source_ref,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        record_audit=True,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload["status"] == "proposal":
            print(f"Calendar dry-run proposal: {payload['title']}")
            print(f"Time: {payload['start']} -> {payload['end']}")
            print(f"Audit ID: {payload.get('audit_id')}")
        else:
            print(f"Calendar dry-run blocked: {payload.get('reason')}")
            print(f"Audit ID: {payload.get('audit_id')}")
    return 0 if payload.get("status") == "proposal" else 1


def cmd_calendar_block_create(args) -> int:
    payload = create_calendar_block(
        kind=args.kind,
        day=args.date,
        window_start=args.window_start,
        window_end=args.window_end,
        duration_minutes=args.duration_minutes,
        label=args.label,
        reason=args.reason,
        confidence=args.confidence,
        source_refs=args.source_ref,
        calendar_name=args.calendar_name,
        live=args.live,
        state_db_path=args.state_db,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        db_path=args.db_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload.get("status") == "created":
            print(f"Calendar block created: {payload.get('title')}")
            print(f"Time: {payload.get('start')} -> {payload.get('end')}")
            print(f"Audit ID: {payload.get('audit_id')}")
            print(f"Idempotency key: {payload.get('idempotency_key')}")
        elif payload.get("status") == "skipped_duplicate":
            print(f"Calendar block skipped duplicate: {payload.get('idempotency_key')}")
            print(f"Audit ID: {payload.get('audit_id')}")
        else:
            print(f"Calendar block create blocked/failed: {payload.get('reason')}")
            print(f"Audit ID: {payload.get('audit_id')}")
    if payload.get("status") in {"created", "skipped_duplicate"}:
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_calendar_block_delete(args) -> int:
    payload = delete_calendar_block(
        idempotency_key=args.idempotency_key,
        calendar_name=args.calendar_name,
        live=args.live,
        state_db_path=args.state_db,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        db_path=args.db_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload.get("status") == "deleted":
            print(f"Calendar block deleted: {payload.get('idempotency_key')}")
            print(f"Audit ID: {payload.get('audit_id')}")
        else:
            print(f"Calendar block delete blocked/failed: {payload.get('reason')}")
            print(f"Audit ID: {payload.get('audit_id')}")
    if payload.get("status") == "deleted":
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_calendar_block_status(args) -> int:
    payload = inspect_calendar_block(
        idempotency_key=args.idempotency_key,
        calendar_name=args.calendar_name,
        state_db_path=args.state_db,
        db_path=args.db_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar block status: {payload.get('status')}")
        print(f"Idempotency key: {payload.get('idempotency_key')}")
        print(f"Calendar matches: {payload.get('calendar_match_count')}")
        print(f"Recommended action: {payload.get('recommended_action')}")
    return 0 if payload.get("status") in {"active_verified", "deleted_verified"} else 1


def cmd_calendar_block_reconcile(args) -> int:
    payload = reconcile_calendar_blocks(
        calendar_name=args.calendar_name,
        mark_stale=args.mark_stale,
        state_db_path=args.state_db,
        ledger_path=args.ledger_path,
        db_path=args.db_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar reconcile: {payload.get('status')}")
        print(f"Checked: {payload.get('checked_count')}")
        print(f"Marked stale: {payload.get('marked_stale_count')}")
    return 0


def cmd_calendar_write_health(args) -> int:
    payload = calendar_write_health(
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar write health: {payload.get('status')}")
        if payload.get("blocked_checks"):
            print(f"Blocked checks: {', '.join(payload.get('blocked_checks'))}")
        if payload.get("audit_id"):
            print(f"Audit ID: {payload.get('audit_id')}")
    return 0 if payload.get("status") == "ok" else 1


def cmd_trainingpeaks_read_path_check(args) -> int:
    payload = probe_trainingpeaks_read_paths(
        calendar_db_path=args.calendar_db_path,
        events_db_path=args.events_db_path,
        webcal_url_file=args.webcal_url_file,
        start_date=args.start_date,
        days_ahead=args.days_ahead,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        recommendation = payload.get("recommendation") or {}
        print(f"TrainingPeaks read path check: {payload.get('status')}")
        print(f"Recommended path: {recommendation.get('recommended_path') or 'none'}")
        print(f"Decision: {recommendation.get('decision')}")
        print(f"Reason: {recommendation.get('reason')}")
    return 0


def cmd_trainingpeaks_ics_preview(args) -> int:
    if args.ics_file:
        payload = preview_ics_file(
            args.ics_file,
            days_ahead=args.days_ahead,
            start_date=args.start_date,
        )
    else:
        payload = preview_webcal_url_file(
            args.webcal_url_file,
            days_ahead=args.days_ahead,
            start_date=args.start_date,
        )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"TrainingPeaks ICS preview: {payload.get('status')}")
        print(f"Workouts: {payload.get('workout_count', 0)}")
        if payload.get("warnings"):
            print(f"Warnings: {', '.join(payload.get('warnings'))}")
    return 0 if payload.get("status") == "ok" else 1


def cmd_training_calendar_proposals(args) -> int:
    payload = build_training_calendar_proposals(
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        target_working_days=args.target_working_days,
        days_ahead=args.days_ahead,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar proposals: {payload.get('status')}")
        print(f"Target date: {payload.get('target_date')}")
        print(f"Selected workouts: {payload.get('selected_workout_count', 0)}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
        for proposal in payload.get("proposals", []):
            workout = proposal.get("workout") or {}
            inference = proposal.get("inference") or {}
            print(
                "- "
                f"{proposal.get('status')}: {workout.get('title')} "
                f"{inference.get('window_start')}->{inference.get('window_end')} "
                f"key={proposal.get('idempotency_key')}"
            )
    return 0 if payload.get("status") in {"proposal", "partial", "no_workout"} else 1


def cmd_training_calendar_book(args) -> int:
    payload = book_training_calendar_proposal(
        idempotency_key=args.idempotency_key,
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        target_working_days=args.target_working_days,
        days_ahead=args.days_ahead,
        calendar_name=args.calendar_name,
        live=args.live,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar booking: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Idempotency key: {payload.get('idempotency_key')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
        if payload.get("audit_id"):
            print(f"Audit ID: {payload.get('audit_id')}")
    if payload.get("status") in {"created", "skipped_duplicate"}:
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_training_calendar_scheduler_run(args) -> int:
    payload = run_training_calendar_scheduler(
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        target_working_days=args.target_working_days,
        days_ahead=args.days_ahead,
        calendar_name=args.calendar_name,
        max_bookings=args.max_bookings,
        live=args.live,
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
        reconcile=args.reconcile,
        fix_safe=args.fix_safe,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar scheduler: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Target date: {payload.get('target_date')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
        if payload.get("audit_id"):
            print(f"Audit ID: {payload.get('audit_id')}")
    if payload.get("status") in {
        "created",
        "skipped_duplicate",
        "skipped_no_workout",
        "skipped_weekend_target",
        "dry_run_proposal",
    }:
        return 0
    return 1


def cmd_training_calendar_reconcile(args) -> int:
    payload = reconcile_training_calendar(
        webcal_url_file=args.webcal_url_file,
        planning_date=args.planning_date,
        days_ahead=args.days_ahead,
        calendar_name=args.calendar_name,
        fix_safe=args.fix_safe,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Training calendar reconcile: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
    return 1 if payload.get("status") in {"blocked", "failed", "manual_review_required"} else 0


def cmd_email_triage_proposals(args) -> int:
    payload = build_email_triage_proposals(
        planning_date=args.planning_date,
        hours=args.hours,
        limit=args.limit,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Email triage proposals: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Target date: {payload.get('target_date')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
        if payload.get("idempotency_key"):
            print(f"Idempotency key: {payload.get('idempotency_key')}")
    return 0 if payload.get("status") in {"proposal", "skipped_no_attention_emails", "skipped_duplicate"} else 1


def cmd_email_triage_book(args) -> int:
    payload = book_email_triage_proposal(
        idempotency_key=args.idempotency_key,
        planning_date=args.planning_date,
        calendar_name=args.calendar_name,
        live=args.live,
        hours=args.hours,
        limit=args.limit,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Email triage booking: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Idempotency key: {payload.get('idempotency_key')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
    if payload.get("status") in {"created", "skipped_duplicate"}:
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_email_triage_scheduler_run(args) -> int:
    payload = run_email_triage_scheduler(
        planning_date=args.planning_date,
        calendar_name=args.calendar_name,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        hours=args.hours,
        limit=args.limit,
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Email triage scheduler: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
        print(f"Target date: {payload.get('target_date')}")
        print(f"Calendar write attempted: {payload.get('calendar_write_attempted')}")
        if payload.get("audit_id"):
            print(f"Audit ID: {payload.get('audit_id')}")
    if payload.get("status") in {
        "created",
        "skipped_duplicate",
        "skipped_no_attention_emails",
        "skipped_weekend_target",
        "skipped_before_morning",
        "dry_run_proposal",
    }:
        return 0
    return 1


def cmd_assistant_notification_dispatch(args) -> int:
    payload = dispatch_failure_notification(
        {
            "status": args.status,
            "reason": args.reason,
            "target_date": args.target_date,
            "idempotency_key": args.idempotency_key,
        },
        channel_id=args.channel_id,
        config_path=args.config_path,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        dry_run=args.dry_run,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Assistant notification: {payload.get('status')}")
        print(f"Reason: {payload.get('reason')}")
    return 0 if payload.get("status") in {"posted", "dry_run", "skipped"} else 1


def cmd_notion_task_health(args) -> int:
    payload = notion_task_health()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Notion task health: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") == "ok" else 1


def cmd_notion_task_schema_ensure(args) -> int:
    payload = ensure_task_database_schema(live=args.live)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Notion task schema: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "created", "dry_run"} else 1


def cmd_notion_task_list(args) -> int:
    payload = list_open_tasks(limit=args.limit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Notion tasks: {payload.get('status')} count={len(payload.get('tasks') or [])}")
        for task in (payload.get("tasks") or [])[: args.limit]:
            print(f"- {task.get('priority')}: {task.get('title')}")
    return 0 if payload.get("status") == "ok" else 1


def cmd_task_detect(args) -> int:
    signals = collect_task_signals(sources=args.sources, since_days=args.since_days, limit=args.limit)
    detected = detect_task_candidates(signals.get("signals") or [], use_llm=not args.no_llm, max_candidates=args.limit)
    deduped = dedupe_task_candidates(detected.get("candidates") or [])
    identities = resolve_task_identities(deduped.get("candidates") or [], existing_tasks=[])
    payload = {
        "status": "ok" if signals.get("status") == "ok" and detected.get("status") == "ok" else "degraded",
        "signals": {"status": signals.get("status"), "signal_count": signals.get("signal_count"), "errors": signals.get("errors")},
        "detected": detected,
        "deduped": deduped,
        "identity": identities,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task detection: {payload['status']} candidates={deduped.get('candidate_count')}")
    return 0 if payload["status"] in {"ok", "degraded"} else 1


def cmd_task_detector_llm_health(args) -> int:
    payload = task_llm_health()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task detector LLM: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") == "healthy" else 1


def cmd_task_reminders_run(args) -> int:
    payload = run_task_reminders(
        today=args.today,
        notify=args.notify,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        ledger_path=args.ledger_path,
        scheduler_db_path=args.scheduler_db,
        live=getattr(args, "live", False),
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task reminders: {payload.get('status')} count={payload.get('reminder_count', 0)}")
    return 0 if payload.get("status") in {"ok", "skipped_no_reminders"} else 1



def cmd_task_lifecycle_run(args) -> int:
    payload = run_task_lifecycle(
        today=args.today,
        live=args.live,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task lifecycle: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"updated", "dry_run", "skipped_no_due_tasks"} else 1


def cmd_task_focus_proposals(args) -> int:
    payload = build_task_focus_proposals(
        planning_date=args.planning_date,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task focus proposals: {payload.get('status')} ({payload.get('reason')})")
        if payload.get("idempotency_key"):
            print(f"Idempotency key: {payload.get('idempotency_key')}")
    return 0 if payload.get("status") in {"proposal", "skipped_no_focus_tasks", "skipped_duplicate", "skipped_weekend_target"} else 1


def cmd_task_focus_book(args) -> int:
    payload = book_task_focus_proposal(
        idempotency_key=args.idempotency_key,
        planning_date=args.planning_date,
        calendar_name=args.calendar_name,
        live=args.live,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task focus booking: {payload.get('status')} ({payload.get('reason')})")
    if payload.get("status") in {"created", "skipped_duplicate"}:
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_coding_signal_sync(args) -> int:
    payload = run_coding_signal_sync(
        output_path=args.output_path,
        remote_host=args.remote_host,
        remote_path=args.remote_path,
        push=args.push,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding signal sync: {payload.get('status')} ({(payload.get('remote_sync') or {}).get('status')})")
    return 0 if payload.get("status") == "ok" else 1


def cmd_coding_signal_inspect(args) -> int:
    payload = inspect_coding_signals(
        laptop_manifest_path=args.laptop_manifest_path,
        include_local_sessions=args.include_local_sessions,
        include_repos=args.include_repos,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding signals: {payload.get('status')} count={payload.get('signal_count')}")
    return 0 if payload.get("status") in {"ok", "empty"} else 1


def cmd_coding_memory_enrich(args) -> int:
    payload = enrich_project_memory(args.project, title=args.title, limit=args.limit)
    payload = {**payload, "workflow": "coding_memory_enricher", "calendar_write_attempted": False}
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"Coding memory enrichment: {payload.get('status')} "
            f"refs={len(payload.get('memory_refs') or [])}"
        )
    return 0 if payload.get("status") in {"ok", "empty", "skipped"} else 1


def cmd_coding_work_briefing(args) -> int:
    payload = build_coding_work_briefing(
        planning_date=args.planning_date,
        laptop_manifest_path=args.laptop_manifest_path,
        use_llm=not args.no_llm,
        use_memory=getattr(args, "use_memory", True),
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload.get("briefing") or f"Coding briefing: {payload.get('status')}")
    return 0 if payload.get("status") in {"ok", "empty"} else 1


def cmd_coding_focus_proposals(args) -> int:
    briefing = build_coding_work_briefing(
        planning_date=args.planning_date,
        laptop_manifest_path=args.laptop_manifest_path,
        use_memory=getattr(args, "use_memory", True),
    )
    payload = build_coding_focus_proposals(
        planning_date=args.planning_date,
        briefing_payload=briefing,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        write_audit=args.write_audit,
        max_blocks=args.max_blocks,
    )
    payload["briefing"] = {"status": briefing.get("status"), "work_item_count": briefing.get("work_item_count"), "selected_count": briefing.get("selected_count")}
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding focus proposals: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"proposal", "skipped_no_coding_focus", "skipped_weekend_target"} else 1


def cmd_coding_focus_book(args) -> int:
    payload = book_coding_focus_proposal(
        idempotency_key=args.idempotency_key,
        planning_date=args.planning_date,
        calendar_name=args.calendar_name,
        live=args.live,
        db_path=args.db_path,
        state_db_path=args.state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding focus booking: {payload.get('status')} ({payload.get('reason')})")
    if payload.get("status") in {"created", "skipped_duplicate"}:
        return 0
    return 2 if payload.get("reason") == "live_flag_required" else 1


def cmd_coding_work_scheduler_run(args) -> int:
    payload = run_coding_work_scheduler(
        planning_date=args.planning_date,
        live=args.live,
        notify=args.notify,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        laptop_manifest_path=args.laptop_manifest_path,
        max_blocks=args.max_blocks,
        use_memory=getattr(args, "use_memory", True),
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding work scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "skipped_weekend_target", "skipped_no_coding_focus", "skipped_duplicate_run"} else 1


def cmd_coding_work_llm_health(args) -> int:
    payload = task_llm_health()
    payload = {**payload, "workflow": "coding_work_briefing", "calendar_write_attempted": False}
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding work LLM: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") == "healthy" else 1


def cmd_task_command_apply(args) -> int:
    payload = apply_task_command(
        args.text,
        source=args.source,
        source_ref=args.source_ref,
        live=args.live,
        use_llm=not getattr(args, "no_llm", False),
        ledger_path=getattr(args, "ledger_path", None),
        write_audit=getattr(args, "write_audit", True),
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task command: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"created", "updated", "dry_run"} else 1


def cmd_meeting_task_signals(args) -> int:
    payload = collect_meeting_task_signals(
        meeting_dir=args.meeting_dir or "/Users/clawdbot/Documents/VAULT/Rocky/OpenClaw Memory/meetings",
        since_days=args.since_days,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Meeting task signals: {payload.get('status')} ({payload.get('signal_count', 0)} signals)")
    return 0 if payload.get("status") in {"ok", "degraded"} else 1


def cmd_task_command_capture_run(args) -> int:
    payload = run_task_command_capture_scheduler(
        sources=args.sources,
        live=args.live,
        notify_failures=args.notify_failures,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        since_minutes=args.since_minutes,
        limit=args.limit,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task command capture: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_duplicate_run"} else 1


def cmd_task_spine_scheduler_run(args) -> int:
    payload = run_task_spine_scheduler(
        planning_date=args.planning_date,
        live=args.live,
        notify=args.notify,
        notification_dry_run=args.notification_dry_run,
        notification_channel_id=args.notification_channel_id,
        sources=args.sources,
        since_days=args.since_days,
        limit=args.limit,
        db_path=args.db_path,
        calendar_state_db_path=args.calendar_state_db,
        scheduler_db_path=args.scheduler_db,
        ledger_path=args.ledger_path,
        state_file=args.state_file,
        lock_ttl_seconds=args.lock_ttl_seconds,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Task spine scheduler: {payload.get('status')} ({payload.get('reason')})")
    return 0 if payload.get("status") in {"ok", "degraded", "skipped_duplicate_run"} else 1


def cmd_calendar_tcc_probe(args) -> int:
    payload = build_calendar_tcc_probe(db_path=args.db_path)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Calendar TCC probe: {payload.get('status')}")
        print(f"Failure class: {payload.get('failure_class')}")
        print(f"Context: {(payload.get('context') or {}).get('execution_context')}")
        print(f"Recommendation: {payload.get('recommendation')}")
    return 0 if payload.get("status") == "ok" else 1


def cmd_agentmail_bridge_health(args) -> int:
    payload = build_agentmail_bridge_health(
        run_tests=getattr(args, "run_tests", False),
        read_launchctl=getattr(args, "read_launchctl", True),
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"AgentMail bridge: {payload.get('status')} ({payload.get('recommendation')})")
        for item in payload.get("files", []):
            print(f"- {item.get('path')}: {item.get('status')}")
    return 0 if payload.get("status") in {"ok", "degraded"} else 1


def cmd_assistant_scheduler_health(args) -> int:
    payload = evaluate_all_scheduler_jobs(
        job_name=args.job,
        state_db_path=args.state_db,
        audit_log_path=args.audit_ledger,
        write_state=True,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_scheduler_health_report(payload))
    return 1 if payload.get("status") == "blocked" else 0


def cmd_assistant_dead_letters(args) -> int:
    state = AssistantSchedulerState(args.state_db)
    status = None if args.status == "all" else args.status
    records = state.list_dead_letters(status=status, limit=args.limit)
    if args.json_output:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    if not records:
        print("No assistant dead letters found.")
        return 0
    print(f"Assistant Dead Letters (latest {len(records)})")
    print("=" * 60)
    for record in records:
        print(
            f"[{record.get('status')}] {record.get('dead_letter_id')} "
            f"{record.get('job_name')} {record.get('failure_class')}"
        )
        print(f"     {record.get('safe_summary')}")
    return 0


def cmd_assistant_lock_smoke(args) -> int:
    payload = smoke_lock_cycle(
        workflow=args.workflow,
        idempotency_key=args.idempotency_key,
        ttl_seconds=args.ttl_seconds,
        db_path=args.state_db,
        ledger_path=args.audit_ledger,
        write_audit=args.write_audit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Assistant lock smoke: {payload['status']}")
        print(f"First: {payload['first']['status']}")
        print(f"Second: {payload['second']['status']}")
    return 0 if payload.get("status") == "ok" else 1


def cmd_runtime_version(args) -> int:
    payload = runtime_version_payload()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_runtime_version(payload), end="")
    return 0


def cmd_runtime_answer(args) -> int:
    payload = build_runtime_answer(
        args.prompt,
        read_only=args.read_only,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload["status"] == "ok":
            print(payload["answer"])
        else:
            print(f"Runtime answer {payload['status']}: {'; '.join(payload.get('errors') or [])}", file=sys.stderr)
    return 0 if payload["status"] == "ok" else 1


def cmd_work_signal_summary(args) -> int:
    payload = build_work_signal_summary(
        source=args.source,
        project=args.project,
        since=args.since,
        read_only=args.read_only,
        limit=args.limit,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_work_signal_summary(payload), end="")
    return 0 if payload["status"] in {"ok", "partial"} else 1


def cmd_lesson_capture(args) -> int:
    try:
        payload = capture_lesson(
            workflow=args.workflow,
            symptom=args.symptom,
            root_cause=args.root_cause,
            safer_future_behavior=args.safer_future_behavior,
            status=args.status,
            confidence=args.confidence,
            source_ref=args.source_ref,
            tags=args.tag,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        lesson = payload["lesson"]
        print(f"Lesson recorded for {lesson['workflow']}: {lesson['safer_future_behavior']}")
    return 0


def cmd_lesson_recent(args) -> int:
    lessons = read_recent_lessons(limit=args.limit)
    if args.json_output:
        print(json.dumps(lessons, indent=2, ensure_ascii=False))
    else:
        print(format_recent_lessons(lessons))
    return 0


def cmd_memory_promote(args) -> int:
    try:
        payload = promote_cross_agent_memory(
            agent_name=args.agent_name,
            note_type=args.note_type,
            title=args.title,
            summary=args.summary,
            body=args.body,
            tags=args.tag,
            source_ref=args.source_ref,
            source_date=args.source_date,
            dedupe_key=args.dedupe_key,
            related_entities=args.related_entities,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload["status"] == "ok":
            print(f"Memory promotion {payload['action']}: {payload['path']}")
        else:
            print(f"Memory promotion skipped: {payload.get('reason', 'unknown reason')}")
    return 0 if payload["status"] in {"ok", "skipped"} else 1


def cmd_memory_recent_promotions(args) -> int:
    events = read_recent_promotions(limit=args.limit)
    if args.json_output:
        print(json.dumps(events, indent=2, ensure_ascii=False))
    else:
        print(format_recent_promotions(events))
    return 0


def cmd_obsidian_status(args) -> int:
    payload = get_obsidian_status()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_obsidian_status(payload))
    return 0 if payload["status"] in {"ready", "disabled", "missing", "unconfigured"} else 1


def cmd_obsidian_sync(args) -> int:
    payload = sync_obsidian_vault()
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_obsidian_status(payload["status_snapshot"]))
        if payload["status"] == "ok":
            print("Sync result: ok")
        else:
            detail = payload.get("reason") or payload.get("warning") or payload["status"]
            print(f"Sync result: {payload['status']} ({detail})")
    return 0 if payload["status"] in {"ok", "skipped"} else 1


def cmd_obsidian_query(args) -> int:
    payload = query_obsidian_vault(
        args.query,
        limit=args.limit,
        mode=args.mode,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_obsidian_query(payload))
    return 0 if payload["status"] in {"ok", "skipped"} else 1


def cmd_obsidian_write(args) -> int:
    content = args.content
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    if not content:
        print("[ERROR] Provide --content or --content-file.", file=sys.stderr)
        return 1
    payload = write_obsidian_note(
        args.title,
        content=content,
        append=args.append,
    )
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if payload["status"] == "ok":
            print(f"Obsidian note written: {payload['path']}")
        else:
            print(f"Obsidian write skipped: {payload.get('reason', 'unknown reason')}")
    return 0 if payload["status"] in {"ok", "skipped"} else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "health-report": cmd_health_report,
        "capability-report": cmd_capability_report,
        "response-check-fixtures": cmd_response_check_fixtures,
        "show-recent-runs": cmd_show_recent_runs,
        "show-bob-jobs": cmd_show_bob_jobs,
        "show-bob-reports": cmd_show_bob_reports,
        "report-bob-stage": cmd_report_bob_stage,
        "mark-bob-dusan-notified": cmd_mark_bob_dusan_notified,
        "show-subagent-failures": cmd_show_subagent_failures,
        "recover-subagent-failures": cmd_recover_subagent_failures,
        "archive-summary": cmd_archive_summary,
        "assistant-audit-recent": cmd_assistant_audit_recent,
        "calendar-policy-check": cmd_calendar_policy_check,
        "calendar-dry-run": cmd_calendar_dry_run,
        "calendar-block-create": cmd_calendar_block_create,
        "calendar-block-delete": cmd_calendar_block_delete,
        "calendar-block-status": cmd_calendar_block_status,
        "calendar-block-reconcile": cmd_calendar_block_reconcile,
        "calendar-write-health": cmd_calendar_write_health,
        "calendar-tcc-probe": cmd_calendar_tcc_probe,
        "trainingpeaks-read-path-check": cmd_trainingpeaks_read_path_check,
        "trainingpeaks-ics-preview": cmd_trainingpeaks_ics_preview,
        "training-calendar-proposals": cmd_training_calendar_proposals,
        "training-calendar-book": cmd_training_calendar_book,
        "training-calendar-scheduler-run": cmd_training_calendar_scheduler_run,
        "training-calendar-reconcile": cmd_training_calendar_reconcile,
        "email-triage-proposals": cmd_email_triage_proposals,
        "email-triage-book": cmd_email_triage_book,
        "email-triage-scheduler-run": cmd_email_triage_scheduler_run,
        "notion-task-health": cmd_notion_task_health,
        "notion-task-schema-ensure": cmd_notion_task_schema_ensure,
        "notion-task-list": cmd_notion_task_list,
        "task-detect": cmd_task_detect,
        "meeting-task-signals": cmd_meeting_task_signals,
        "task-detector-llm-health": cmd_task_detector_llm_health,
        "task-reminders-run": cmd_task_reminders_run,
        "task-lifecycle-run": cmd_task_lifecycle_run,
        "task-focus-proposals": cmd_task_focus_proposals,
        "task-focus-book": cmd_task_focus_book,
        "coding-signal-sync": cmd_coding_signal_sync,
        "coding-signal-inspect": cmd_coding_signal_inspect,
        "coding-memory-enrich": cmd_coding_memory_enrich,
        "coding-work-briefing": cmd_coding_work_briefing,
        "coding-focus-proposals": cmd_coding_focus_proposals,
        "coding-focus-book": cmd_coding_focus_book,
        "coding-work-scheduler-run": cmd_coding_work_scheduler_run,
        "coding-work-llm-health": cmd_coding_work_llm_health,
        "task-command-apply": cmd_task_command_apply,
        "task-command-capture-run": cmd_task_command_capture_run,
        "task-spine-scheduler-run": cmd_task_spine_scheduler_run,
        "assistant-notification-dispatch": cmd_assistant_notification_dispatch,
        "agentmail-bridge-health": cmd_agentmail_bridge_health,
        "assistant-scheduler-health": cmd_assistant_scheduler_health,
        "assistant-dead-letters": cmd_assistant_dead_letters,
        "assistant-lock-smoke": cmd_assistant_lock_smoke,
        "runtime-version": cmd_runtime_version,
        "runtime-answer": cmd_runtime_answer,
        "work-signal-summary": cmd_work_signal_summary,
        "lesson-capture": cmd_lesson_capture,
        "lesson-recent": cmd_lesson_recent,
        "memory-promote": cmd_memory_promote,
        "memory-recent-promotions": cmd_memory_recent_promotions,
        "obsidian-status": cmd_obsidian_status,
        "obsidian-sync": cmd_obsidian_sync,
        "obsidian-query": cmd_obsidian_query,
        "obsidian-write": cmd_obsidian_write,
    }
    handler = dispatch.get(args.command)
    if not handler:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
