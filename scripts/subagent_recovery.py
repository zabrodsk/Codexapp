#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from subagent_failure_events import (
    VALIDATION_LIVE_PATH_EXERCISED,
    VALIDATION_NOT_VERIFIED,
    VALIDATION_OUTCOME_CONFIRMED,
    SubagentFailureEvent,
    SubagentFailureLedger,
    build_validation_result,
    render_general_report,
    utc_now_iso,
)


# Dedicated operator/reporting channel for subagent incident notifications.
GENERAL_CHANNEL_ID = "1485710572325703901"
BETTY_MAIL_CHANNEL_ID = "1485021564939276469"
OPENCLAW = "/opt/homebrew/bin/openclaw"
BETTY_HELPER = (
    Path.home()
    / ".openclaw"
    / "workspace-betty"
    / "skills"
    / "apple-mail-control"
    / "scripts"
    / "apple_mail_helper.py"
)
BETTY_RUNNER = Path.home() / ".openclaw" / "workspace-betty" / "scripts" / "run_betty_mail_triage.py"
BETTY_PYTHON = Path.home() / ".openclaw" / "workspace-betty" / ".venv" / "bin" / "python"

PostFunc = Callable[[str, str], dict[str, Any]]
TERMINAL_RECOVERY_STATUSES = {
    "resolved",
    "local_recovery_succeeded",
    "human_required",
    "unrecoverable",
}


def post_via_openclaw(channel_id: str, message: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            OPENCLAW,
            "message",
            "send",
            "--channel",
            "discord",
            "--target",
            f"channel:{channel_id}",
            "--message",
            message,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"openclaw exited {proc.returncode}")
    return {"status": "posted", "channel_id": channel_id, "stdout": proc.stdout.strip()}


def recover_pending_failures(
    *,
    ledger: SubagentFailureLedger | None = None,
    post_func: PostFunc | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    store = ledger or SubagentFailureLedger()
    poster = post_func or post_via_openclaw
    events = [
        event
        for event in store.latest_events().values()
        if event.recovery_status not in TERMINAL_RECOVERY_STATUSES
        or event.general_reported_status != event.recovery_status
        or not event.general_reported_at
    ]
    events.sort(key=lambda item: item.timestamp, reverse=True)
    if limit is not None:
        events = events[:limit]
    processed: list[dict[str, Any]] = []

    for event in events:
        current = attempt_recovery(event, ledger=store, post_func=poster)
        current = report_to_general_if_needed(current, ledger=store, post_func=poster)
        processed.append(asdict(current))

    return {"status": "ok", "processed": processed, "count": len(processed)}


def attempt_recovery(
    event: SubagentFailureEvent,
    *,
    ledger: SubagentFailureLedger,
    post_func: PostFunc,
) -> SubagentFailureEvent:
    if event.recovery_status in TERMINAL_RECOVERY_STATUSES:
        return event
    if not event.recoverable or not event.retry_recommended:
        return ledger.update(
            event.event_id,
            recovery_status="unrecoverable",
            validation_result=build_validation_result(
                VALIDATION_NOT_VERIFIED,
                not_verified=["No safe automatic recovery rule matched this event."],
            ),
        )
    if event.recovery_attempts >= 1:
        return ledger.update(
            event.event_id,
            recovery_status="human_required",
            validation_result=build_validation_result(
                VALIDATION_NOT_VERIFIED,
                not_verified=["Automatic recovery was not retried after the first attempt."],
            ),
        )

    try:
        if _is_delivery_retry(event):
            result = _retry_delivery(event, post_func=post_func)
            return ledger.update(
                event.event_id,
                recovery_status="resolved",
                recovery_attempts=event.recovery_attempts + 1,
                last_recovery_error=None,
                resolved_at=utc_now_iso(),
                validation_result=build_validation_result(
                    VALIDATION_OUTCOME_CONFIRMED,
                    evidence=[
                        f"Delivery retry returned success for channel {event.delivery_channel}.",
                        _post_result_summary(result),
                    ],
                ),
            )
        if _is_betty_mail_healthcheck(event):
            result = _rerun_betty_mail_triage(event)
            return ledger.update(
                event.event_id,
                recovery_status="local_recovery_succeeded",
                recovery_attempts=event.recovery_attempts + 1,
                last_recovery_error=None,
                validation_result=build_validation_result(
                    VALIDATION_LIVE_PATH_EXERCISED,
                    evidence=[
                        "Betty mail triage rerun command exited 0.",
                        _command_result_summary(result),
                    ],
                    not_verified=[
                        "Rocky did not independently confirm Apple Mail produced the intended triage output.",
                        "Rocky did not independently confirm the Betty report reached the native Discord channel.",
                    ],
                ),
            )
        return ledger.update(
            event.event_id,
            recovery_status="unrecoverable",
            validation_result=build_validation_result(
                VALIDATION_NOT_VERIFIED,
                not_verified=["No automatic recovery rule matched this event."],
            ),
        )
    except Exception as exc:
        return ledger.update(
            event.event_id,
            recovery_status="human_required",
            recovery_attempts=event.recovery_attempts + 1,
            last_recovery_error=str(exc),
            validation_result=build_validation_result(
                VALIDATION_LIVE_PATH_EXERCISED,
                evidence=["Rocky attempted an automatic recovery step."],
                not_verified=[f"Recovery attempt failed before outcome verification: {exc}"],
            ),
        )


def report_to_general_if_needed(
    event: SubagentFailureEvent,
    *,
    ledger: SubagentFailureLedger,
    post_func: PostFunc,
) -> SubagentFailureEvent:
    if event.general_reported_status == event.recovery_status and event.general_reported_at:
        return event
    post_func(GENERAL_CHANNEL_ID, render_general_report(event))
    return ledger.update(
        event.event_id,
        general_reported_at=utc_now_iso(),
        general_reported_status=event.recovery_status,
    )


def _is_delivery_retry(event: SubagentFailureEvent) -> bool:
    return event.status == "delivery_failed" and bool(event.delivery_channel)


def _retry_delivery(event: SubagentFailureEvent, *, post_func: PostFunc) -> dict[str, Any]:
    message = _artifact_message(event)
    if not message:
        raise RuntimeError("No local artifact or report text available for delivery retry.")
    return post_func(str(event.delivery_channel), message)


def _artifact_message(event: SubagentFailureEvent) -> str:
    artifacts = event.artifacts or {}
    if artifacts.get("report_text"):
        return str(artifacts["report_text"])
    local_copy = artifacts.get("local_copy") if isinstance(artifacts.get("local_copy"), dict) else {}
    for key in ("markdown_path", "latest_markdown_path"):
        path_text = local_copy.get(key)
        if not path_text:
            continue
        path = Path(str(path_text)).expanduser()
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def _is_betty_mail_healthcheck(event: SubagentFailureEvent) -> bool:
    dependency = (event.blocked_dependency or "").lower()
    message = (event.error_message or "").lower()
    return event.agent == "betty" and ("apple mail" in dependency or "envelope index" in message)


def _rerun_betty_mail_triage(event: SubagentFailureEvent) -> dict[str, Any]:
    channel_id = str(event.delivery_channel or BETTY_MAIL_CHANNEL_ID)
    proc = subprocess.run(
        [
            str(BETTY_PYTHON),
            str(BETTY_RUNNER),
            "--hours",
            "36",
            "--limit",
            "50",
            "--channel-id",
            channel_id,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"Betty triage rerun exited {proc.returncode}")
    return {
        "status": "command_exited_0",
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _post_result_summary(result: dict[str, Any] | None) -> str:
    if not result:
        return "Delivery post function returned no details."
    status = result.get("status") or "unknown"
    channel_id = result.get("channel_id") or "unknown-channel"
    return f"Post result status={status} channel={channel_id}."


def _command_result_summary(result: dict[str, Any] | None) -> str:
    if not result:
        return "Command returned no details."
    stdout = " ".join(str(result.get("stdout") or "").split())
    stderr = " ".join(str(result.get("stderr") or "").split())
    if stdout:
        return f"Command stdout: {stdout[:180]}"
    if stderr:
        return f"Command stderr: {stderr[:180]}"
    return str(result.get("status") or "Command completed.")
