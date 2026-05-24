#!/usr/bin/env python3
"""Render Rocky's weekly personal review for Discord."""
from __future__ import annotations

import hashlib
import re
from typing import Any

SENSITIVE_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)
MAX_DISCORD_CHARS = 1900


def render_weekly_personal_review(signals: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    week = signals.get("week_label") or plan.get("week_label") or "this week"
    lines = [
        f"Rocky weekly review - {week}",
        "",
        "Last week",
        *_bullets(plan.get("last_week") or [], empty="No prior-week assistant evidence surfaced."),
        "",
        "This week",
        *_bullets(plan.get("this_week") or [], empty="Follow the daily briefs and existing calendar."),
        "",
        "Protect",
        *_bullets(plan.get("protect") or [], empty="No additional weekly protection recommendation."),
        "",
        "Do first",
        *_bullets(plan.get("do_first") or [], empty="No urgent weekly priority found."),
        "",
        "Risks / overloaded days",
        *_bullets(plan.get("risks_or_overloaded_days") or [], empty="No major risks surfaced."),
        "",
        "Open loops",
        *_bullets(plan.get("open_loops") or [], empty="No high-confidence open loop surfaced."),
        "",
        "Calendar hygiene",
        f"- {_hygiene_text(plan.get('calendar_hygiene') or {})}",
        "",
        "What Rocky handled",
        *_plain_bullets(plan.get("what_rocky_handled") or [], empty="No autonomous weekly actions completed."),
        "",
        "Learning / calibration",
        f"- {_learning_text(plan.get('learning_or_calibration') or {})}",
        "",
        "Recommended adjustments",
        *_bullets(plan.get("recommended_adjustments") or [], empty="No adjustment recommended this week."),
    ]
    message = _safe_multiline("\n".join(lines)).strip()
    if len(message) > MAX_DISCORD_CHARS:
        message = message[: MAX_DISCORD_CHARS - 80].rstrip() + "\n...\nWeekly review truncated safely."
    return {"status": "ok", "week_label": week, "discord_message": message, "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16], "message_chars": len(message)}


def _bullets(items: list[dict[str, Any]], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {_item_text(item)}" for item in items[:8]]


def _plain_bullets(items: list[Any], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {_safe_inline(item, 220)}" for item in items[:8]]


def _item_text(item: dict[str, Any]) -> str:
    category = item.get("category")
    title = item.get("title") or "item"
    reason = item.get("reason")
    ref = item.get("task_ref") or item.get("source_ref") or item.get("idempotency_key")
    base = f"{category}: {title}" if category else str(title)
    if reason:
        base += f" ({reason})"
    if ref:
        base += f" [{ref}]"
    return _safe_inline(base, 260)


def _hygiene_text(payload: dict[str, Any]) -> str:
    issue_count = int(payload.get("issue_count") or 0)
    summary = payload.get("summary") or payload.get("status") or "unknown"
    return _safe_inline(f"{issue_count} issue(s): {summary}", 240)


def _learning_text(payload: dict[str, Any]) -> str:
    return _safe_inline(f"{payload.get('status') or 'unknown'}; active={payload.get('active_bounded_count', 0)}; proposals={payload.get('proposal_count', 0)}; outcomes={payload.get('outcome_count', 0)}", 240)


def _safe_inline(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    text = SENSITIVE_RE.sub("[redacted]", text)
    return text[:limit]


def _safe_multiline(value: Any, limit: int | None = None) -> str:
    lines = []
    for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(re.sub(r"[ \t]+", " ", SENSITIVE_RE.sub("[redacted]", line)).strip())
    text = "\n".join(lines).strip()
    return text[:limit] if limit else text
