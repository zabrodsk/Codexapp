#!/usr/bin/env python3
"""Estimate manual email triage time from sanitized attention counts."""
from __future__ import annotations

import math
from typing import Any

from assistant_preference_models import active_email_duration_multiplier

MINIMUM_MINUTES = 30
ROUND_TO_MINUTES = 15
MAXIMUM_MINUTES = 90
PRIORITY_MINUTES = {
    "urgent": 12,
    "soon": 10,
    "later": 6,
    "waiting": 4,
}
DEFAULT_IMPORTANT_MINUTES = 6


def estimate_email_triage_minutes(
    payload: dict[str, Any],
    *,
    preferences: dict[str, Any] | None = None,
    learning_db_path: str | None = None,
) -> dict[str, Any]:
    """Return a conservative v1 time estimate from sanitized email counts."""
    attention_count = int(payload.get("attention_count") or 0)
    priority_buckets = {str(key): int(value or 0) for key, value in (payload.get("priority_buckets") or {}).items()}
    if attention_count <= 0:
        return {
            "estimated_minutes": 0,
            "raw_estimated_minutes": 0,
            "adjusted_raw_estimated_minutes": 0,
            "minimum_minutes": MINIMUM_MINUTES,
            "maximum_minutes": MAXIMUM_MINUTES,
            "round_to_minutes": ROUND_TO_MINUTES,
            "confidence": "high",
            "reason": "no_attention_emails",
            "priority_buckets": priority_buckets,
            "learning_preference": None,
        }

    raw = 0
    counted = 0
    for priority, count in priority_buckets.items():
        if priority == "ignore":
            continue
        raw += PRIORITY_MINUTES.get(priority, DEFAULT_IMPORTANT_MINUTES) * count
        counted += count
    if counted < attention_count:
        raw += DEFAULT_IMPORTANT_MINUTES * (attention_count - counted)

    preference = active_email_duration_multiplier(db_path=learning_db_path, preferences=preferences)
    adjusted_raw = raw * float(preference["value"]) if preference else raw
    rounded = int(math.ceil(max(adjusted_raw, MINIMUM_MINUTES) / ROUND_TO_MINUTES) * ROUND_TO_MINUTES)
    estimated = min(MAXIMUM_MINUTES, rounded)
    return {
        "estimated_minutes": estimated,
        "raw_estimated_minutes": raw,
        "adjusted_raw_estimated_minutes": adjusted_raw,
        "minimum_minutes": MINIMUM_MINUTES,
        "maximum_minutes": MAXIMUM_MINUTES,
        "round_to_minutes": ROUND_TO_MINUTES,
        "confidence": "medium",
        "reason": "priority_bucket_heuristic_v1" if not preference else "priority_bucket_heuristic_with_bounded_learning",
        "priority_buckets": priority_buckets,
        "learning_preference": preference,
    }
