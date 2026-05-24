#!/usr/bin/env python3
"""Render Rocky's daily personal assistant briefing."""
from __future__ import annotations

import hashlib
import re
from typing import Any

SENSITIVE_TEXT_RE = re.compile(r"(webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|cookie|token|secret|password|credential|auth|Bearer\s+|\bsk-[A-Za-z0-9])", re.IGNORECASE)
MAX_DISCORD_CHARS = 1850


def render_daily_personal_briefing(signals: dict[str, Any], arbitration: dict[str, Any]) -> dict[str, Any]:
    day = signals.get("planning_date") or arbitration.get("planning_date") or "today"
    calendar = signals.get("calendar") or {}
    top = arbitration.get("top_priority") or {}
    lines = [
        f"Rocky daily brief - {day}",
        "",
        "Today",
        f"- Calendar: {calendar.get('event_count', 0)} event(s); free after noon: {_free_windows(calendar.get('free_windows') or [])}",
        f"- Top priority: {_item_text(top)}",
        "",
        "Do first",
        *_bullets(arbitration.get("do_first") or [], empty="Follow existing calendar commitments."),
        "",
        "Protected time",
        *_plain_bullets(arbitration.get("protected_time") or [], empty="No Rocky-protected time found."),
        "",
        "Needs decision",
        *_bullets(arbitration.get("needs_decision") or [], empty="No immediate decision needed."),
        "",
        "Blocked or risky",
        *_bullets(arbitration.get("blocked_or_risky") or [], empty="No blockers surfaced."),
        "",
        "Suggested focus",
        *_focus_bullets(arbitration.get("suggested_focus") or []),
        "",
        "Deferred / did not fit",
        *_bullets(arbitration.get("deferred_or_did_not_fit") or [], empty="Nothing important was deferred by Rocky."),
        "",
        "What Rocky handled",
        *_plain_bullets(arbitration.get("what_rocky_handled") or [], empty="No autonomous actions completed in this brief."),
    ]
    actions = arbitration.get("safe_booking_actions") or []
    if actions:
        lines.extend(["", "Safe booking actions", *_plain_bullets([f"{item.get('action')}: {item.get('idempotency_key')}" for item in actions], empty="none")])
    message = _safe_text("\n".join(lines)).strip()
    if len(message) > MAX_DISCORD_CHARS:
        message = message[: MAX_DISCORD_CHARS - 80].rstrip() + "\n...\nBrief truncated safely."
    return {
        "status": "ok",
        "planning_date": day,
        "discord_message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest()[:16],
        "message_chars": len(message),
    }


def _free_windows(windows: list[dict[str, Any]]) -> str:
    if not windows:
        return "none"
    return "; ".join(f"{item.get('start')}-{item.get('end')} ({item.get('minutes')}m)" for item in windows[:3])


def _bullets(items: list[dict[str, Any]], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {_item_text(item)}" for item in items[:6]]


def _focus_bullets(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- No separate focus recommendation." ]
    lines = []
    for item in items[:5]:
        title = item.get("title") or item.get("category") or "focus"
        next_step = item.get("next_step") or item.get("reason") or "continue"
        lines.append(f"- {_safe_text(title, 120)}: {_safe_text(next_step, 180)}")
    return lines


def _plain_bullets(items: list[Any], *, empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {_safe_text(item, 220)}" for item in items[:6]]


def _item_text(item: dict[str, Any]) -> str:
    category = item.get("category")
    title = item.get("title") or "item"
    reason = item.get("reason")
    if category and reason:
        return f"{category}: {title} ({reason})"
    if category:
        return f"{category}: {title}"
    return str(title)


def _safe_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip() if not isinstance(value, str) else str(value)
    text = SENSITIVE_TEXT_RE.sub("[redacted]", text)
    if limit:
        return text[:limit]
    return text
