#!/usr/bin/env python3
"""
Rocky Run-State Ledger

JSONL-based ledger for tracking workflow execution and delivery status.
Distinguishes local completion from actual delivery, and classifies
failures honestly.

Usage:
    from rocky_run_ledger import RunLedger, normalize_status
    ledger = RunLedger()
    run_id = ledger.start_run("betty_weekly_report", actor="betty", trigger="cron", target="notion")
    # ... workflow executes ...
    ledger.complete_run(run_id)
    # ... delivery attempted ...
    ledger.fail_delivery(run_id, error="HTTP 401 Unauthorized")

Disable: simply don't import/call this module. The ledger file is
append-only JSONL — delete it to reset.
"""

import json
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default ledger location
DEFAULT_LEDGER_PATH = Path(__file__).parent.parent / "improvement" / "run_ledger.jsonl"


# --- Status normalization ---

# Error pattern -> (execution_status, delivery_status, error_class)
ERROR_PATTERNS = [
    # Partial completion (check first — partial messages may contain other error keywords)
    (r"(?i)(\d+ of \d+.*fail|partial|incomplete)",
     "partial", None, None),
    # Auth errors
    (r"(?i)(401|unauthorized|invalid.?token|token.*invalid|token.*expired|auth.*fail|auth.*invalid|auth.*expired)",
     None, "blocked_auth", "auth_expired"),
    # Permission errors
    (r"(?i)(403|forbidden|permission.*denied|access.*denied|not.*shared.*with|could not find page)",
     None, "blocked_permissions", "permission_denied"),
    # Rate limit errors
    (r"(?i)(429|rate.?limit|too many requests|retry after|cooldown)",
     "failed", "blocked_rate_limit", "rate_limited"),
    # Timeout errors
    (r"(?i)(timeout|timed?.?out|exceeded.*time|deadline exceeded)",
     "timed_out", "timed_out", "timeout"),
    # Network errors
    (r"(?i)(connection.*refused|dns.*resolution|network.*error|unreachable|econnrefused|enotfound)",
     "failed", "failed", "network_error"),
]


@dataclass
class RunRecord:
    """A single run record in the ledger."""
    run_id: str
    workflow_name: str
    actor: str
    trigger_source: str
    timestamp_started: str
    timestamp_finished: Optional[str] = None
    execution_status: str = "started"
    delivery_status: Optional[str] = None
    target_system: Optional[str] = None
    error_class: Optional[str] = None
    error_message: Optional[str] = None
    human_summary: Optional[str] = None
    artifacts: Optional[dict] = None


def normalize_status(
    raw_error: Optional[str],
    local_completed: bool = False,
    delivery_attempted: bool = False,
    delivery_succeeded: bool = False,
) -> tuple[str, Optional[str], Optional[str]]:
    """
    Normalize a raw error into (execution_status, delivery_status, error_class).

    Returns:
        Tuple of (execution_status, delivery_status, error_class)
    """
    if raw_error is None and local_completed and delivery_succeeded:
        return ("completed", "delivered", None)

    if raw_error is None and local_completed and not delivery_attempted:
        return ("completed", None, None)

    if raw_error is None and local_completed and delivery_attempted and not delivery_succeeded:
        return ("completed", "failed", "unknown_error")

    if raw_error:
        for pattern, exec_override, deliv_override, err_class in ERROR_PATTERNS:
            if re.search(pattern, raw_error):
                exec_status = exec_override if exec_override else ("completed" if local_completed else "failed")
                deliv_status = deliv_override if delivery_attempted else None
                return (exec_status, deliv_status, err_class)

    # Fallback
    if local_completed:
        if delivery_attempted:
            return ("completed", "failed", "unknown_error")
        return ("completed", None, None)
    return ("failed", None, "unknown_error")


def format_summary(
    workflow_name: str,
    execution_status: str,
    delivery_status: Optional[str],
    target_system: Optional[str],
    error_class: Optional[str],
    error_message: Optional[str] = None,
) -> str:
    """
    Generate a human-readable one-liner distinguishing execution from delivery.
    """
    # Friendly workflow names
    wf_display = workflow_name.replace("_", " ").title()

    # Full success
    if execution_status == "completed" and delivery_status == "delivered":
        target = f" to {target_system.title()}" if target_system else ""
        return f"{wf_display} generated and delivered{target} successfully."

    # Completed locally but delivery failed
    if execution_status == "completed" and delivery_status and delivery_status != "delivered":
        target = f" {target_system.title()}" if target_system else ""
        reason = _error_reason(error_class, error_message)
        return f"{wf_display} generated locally, but{target} delivery failed: {reason}"

    # Execution failed
    if execution_status == "timed_out":
        return f"{wf_display} timed out. Consider breaking the task into smaller steps."

    if execution_status == "partial":
        detail = f" {error_message}" if error_message else ""
        return f"{wf_display} partially completed.{detail}"

    if execution_status == "failed":
        reason = _error_reason(error_class, error_message)
        return f"{wf_display} failed: {reason}"

    # Fallback
    return f"{wf_display}: execution={execution_status}, delivery={delivery_status or 'n/a'}"


def _error_reason(error_class: Optional[str], error_message: Optional[str] = None) -> str:
    """Map error_class to a human reason."""
    reasons = {
        "auth_expired": "auth token expired. Re-authenticate the integration.",
        "auth_invalid": "authentication rejected. Check credentials.",
        "permission_denied": "insufficient permissions. Share the target resource with the integration.",
        "rate_limited": "rate-limited. Retry after cooldown.",
        "timeout": "operation timed out.",
        "network_error": "network error. Check connectivity.",
        "target_not_found": "target not found.",
        "payload_error": "payload error.",
        "unknown_error": "unknown error occurred.",
    }
    if error_class and error_class in reasons:
        return reasons[error_class]
    if error_message:
        # Truncate long messages
        return error_message[:120]
    return "unknown error."


class RunLedger:
    """
    Append-only JSONL ledger for workflow runs.
    """

    def __init__(self, ledger_path: Optional[Path] = None):
        self.path = ledger_path or DEFAULT_LEDGER_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def start_run(
        self,
        workflow_name: str,
        actor: str = "rocky",
        trigger_source: str = "manual",
        target_system: Optional[str] = None,
    ) -> str:
        """Start tracking a new run. Returns run_id."""
        run_id = str(uuid.uuid4())[:8]
        record = RunRecord(
            run_id=run_id,
            workflow_name=workflow_name,
            actor=actor,
            trigger_source=trigger_source,
            timestamp_started=datetime.now(timezone.utc).isoformat(),
            execution_status="started",
            target_system=target_system,
        )
        self._append(record)
        return run_id

    def complete_run(self, run_id: str, artifacts: Optional[dict] = None) -> RunRecord:
        """Mark a run as locally completed."""
        record = self._get_latest(run_id)
        if not record:
            raise ValueError(f"Run {run_id} not found")
        record.execution_status = "completed"
        record.timestamp_finished = datetime.now(timezone.utc).isoformat()
        record.artifacts = artifacts
        record.human_summary = format_summary(
            record.workflow_name, record.execution_status,
            record.delivery_status, record.target_system,
            record.error_class, record.error_message,
        )
        self._append(record)
        return record

    def mark_delivered(self, run_id: str, artifacts: Optional[dict] = None) -> RunRecord:
        """Mark delivery as successful."""
        record = self._get_latest(run_id)
        if not record:
            raise ValueError(f"Run {run_id} not found")
        record.delivery_status = "delivered"
        if artifacts:
            record.artifacts = {**(record.artifacts or {}), **artifacts}
        record.human_summary = format_summary(
            record.workflow_name, record.execution_status,
            record.delivery_status, record.target_system,
            record.error_class, record.error_message,
        )
        self._append(record)
        return record

    def fail_run(self, run_id: str, error: str) -> RunRecord:
        """Mark a run as failed with error classification."""
        record = self._get_latest(run_id)
        if not record:
            raise ValueError(f"Run {run_id} not found")
        record.error_message = error
        exec_status, deliv_status, err_class = normalize_status(
            error, local_completed=False, delivery_attempted=False,
        )
        record.execution_status = exec_status
        record.delivery_status = deliv_status
        record.error_class = err_class
        record.timestamp_finished = datetime.now(timezone.utc).isoformat()
        record.human_summary = format_summary(
            record.workflow_name, record.execution_status,
            record.delivery_status, record.target_system,
            record.error_class, record.error_message,
        )
        self._append(record)
        return record

    def fail_delivery(self, run_id: str, error: str) -> RunRecord:
        """Mark delivery as failed with error classification."""
        record = self._get_latest(run_id)
        if not record:
            raise ValueError(f"Run {run_id} not found")
        record.error_message = error
        _, deliv_status, err_class = normalize_status(
            error, local_completed=True, delivery_attempted=True, delivery_succeeded=False,
        )
        record.delivery_status = deliv_status
        record.error_class = err_class
        record.human_summary = format_summary(
            record.workflow_name, record.execution_status,
            record.delivery_status, record.target_system,
            record.error_class, record.error_message,
        )
        self._append(record)
        return record

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        """Get the latest state of a run."""
        return self._get_latest(run_id)

    def get_recent_runs(self, limit: int = 10) -> list[RunRecord]:
        """Get the most recent runs (latest state per run_id)."""
        all_records = self._read_all()
        # Group by run_id, keep latest
        latest = {}
        for record in all_records:
            latest[record.run_id] = record
        runs = sorted(latest.values(), key=lambda r: r.timestamp_started, reverse=True)
        return runs[:limit]

    def _append(self, record: RunRecord):
        """Append a record to the ledger."""
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def _read_all(self) -> list[RunRecord]:
        """Read all records from the ledger."""
        if not self.path.exists():
            return []
        records = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    records.append(RunRecord(**data))
        return records

    def _get_latest(self, run_id: str) -> Optional[RunRecord]:
        """Get the latest record for a given run_id."""
        records = self._read_all()
        matching = [r for r in records if r.run_id == run_id]
        return matching[-1] if matching else None


if __name__ == "__main__":
    import sys

    fixture_path = (
        Path(__file__).parent.parent
        / "improvement"
        / "fixtures"
        / "run_state_cases.json"
    )

    if not fixture_path.exists():
        print(f"Fixture file not found: {fixture_path}")
        sys.exit(1)

    with open(fixture_path) as f:
        data = json.load(f)

    print("=== Run-State Normalization Test ===\n")
    for case in data["cases"]:
        raw = case["raw_input"]
        expected = case["expected"]
        exec_s, deliv_s, err_c = normalize_status(
            raw.get("raw_error"),
            local_completed=raw.get("local_completed", False),
            delivery_attempted=raw.get("delivery_attempted", False),
            delivery_succeeded=raw.get("delivery_succeeded", False),
        )
        summary = format_summary(
            raw["workflow_name"], exec_s, deliv_s,
            raw.get("target_system"), err_c, raw.get("raw_error"),
        )
        match = (
            exec_s == expected["execution_status"]
            and deliv_s == expected.get("delivery_status")
            and err_c == expected.get("error_class")
        )
        status = "PASS" if match else "FAIL"
        print(f"[{status}] {case['id']}")
        print(f"  Summary: {summary}")
        if not match:
            print(f"  Expected: exec={expected['execution_status']}, deliv={expected.get('delivery_status')}, err={expected.get('error_class')}")
            print(f"  Got:      exec={exec_s}, deliv={deliv_s}, err={err_c}")
        print()
