#!/usr/bin/env python3
"""Sanitized calibration review for Rocky assistant learning."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_store import AssistantLearningStore, DEFAULT_LEARNING_DB_PATH, sanitize_payload

BOUNDED_KEYS = {
    "email_triage.duration_multiplier": {
        "lane": "email_triage",
        "minimum_evidence": 5,
        "minimum_confidence": 0.70,
        "authority": "bounded_duration_estimate",
    },
    "coding_focus.default_duration_minutes": {
        "lane": "coding_focus",
        "minimum_evidence": 5,
        "minimum_confidence": 0.70,
        "authority": "bounded_duration_estimate",
    },
}
LANE_ORDER = [
    "email_triage",
    "coding_focus",
    "task_reminder",
    "task_spine",
    "training",
    "daily_briefing",
    "task_command",
]
REVIEW_ONLY_PREFIXES = {
    "training": "review_only_training_timing",
    "task_reminder": "review_only_reminder_cadence",
    "task_completion": "review_only_task_completion",
    "daily_priority": "review_only_priority_weights",
}


def build_assistant_learning_calibration_review(*, learning_db_path: str | Path | None = None, limit: int = 1000) -> dict[str, Any]:
    path = Path(learning_db_path) if learning_db_path else DEFAULT_LEARNING_DB_PATH
    if not path.exists():
        return {
            "status": "calibration_pending",
            "reason": "assistant_learning_store_missing",
            "lanes": [],
            "bounded_preferences": [],
            "review_only_proposals": [],
            "calendar_write_attempted": False,
            "notion_write_attempted": False,
        }
    store = AssistantLearningStore(path)
    outcomes = store.list_outcomes(limit=limit)
    preferences = store.list_preference_models()
    proposals = store.list_learning_proposals(status="proposed", limit=50)
    lane_summaries = _lane_summaries(outcomes)
    bounded = [_bounded_review(pref) for pref in preferences if pref.get("preference_key") in BOUNDED_KEYS]
    review_only = [_proposal_review(proposal) for proposal in proposals]
    active_count = sum(1 for item in bounded if item.get("status") == "active_bounded")
    status = "ok" if active_count else "calibration_pending"
    payload = {
        "status": status,
        "reason": "active_bounded_preferences_available" if active_count else "insufficient_evidence_expected",
        "outcome_count": len(outcomes),
        "active_bounded_count": active_count,
        "lanes": lane_summaries,
        "bounded_preferences": bounded,
        "review_only_proposals": review_only,
        "safety": {
            "calendar_policy_unchanged": True,
            "friday_saturday_sunday_guardrail_unchanged": True,
            "review_only_proposals_auto_applied": False,
        },
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }
    return sanitize_payload(payload)


def _lane_summaries(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for row in outcomes:
        lane = str(row.get("lane") or "unknown")
        bucket = counts.setdefault(lane, {"lane": lane, "outcome_count": 0, "statuses": {}, "latest_observed_at": None})
        bucket["outcome_count"] += 1
        status = str(row.get("outcome_status") or "unknown")
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        observed = str(row.get("observed_at") or "")
        if observed and (not bucket["latest_observed_at"] or observed > bucket["latest_observed_at"]):
            bucket["latest_observed_at"] = observed
    ordered = []
    for lane in LANE_ORDER:
        if lane in counts:
            ordered.append(counts.pop(lane))
    ordered.extend(sorted(counts.values(), key=lambda item: item["lane"]))
    return ordered


def _bounded_review(pref: dict[str, Any]) -> dict[str, Any]:
    key = str(pref.get("preference_key") or "")
    rules = BOUNDED_KEYS[key]
    evidence = int(pref.get("evidence_count") or 0)
    confidence = float(pref.get("confidence") or 0)
    active = pref.get("status") == "active_bounded"
    missing = []
    if evidence < rules["minimum_evidence"]:
        missing.append("more_evidence")
    if confidence < rules["minimum_confidence"]:
        missing.append("higher_confidence")
    return {
        "preference_key": key,
        "lane": rules["lane"],
        "status": pref.get("status"),
        "value": pref.get("value"),
        "confidence": confidence,
        "evidence_count": evidence,
        "application_scope": rules["authority"],
        "activation": "active" if active else "pending",
        "reason": pref.get("reason"),
        "needed_for_activation": missing,
        "bounds": pref.get("bounds") or {},
    }


def _proposal_review(proposal: dict[str, Any]) -> dict[str, Any]:
    key = str(proposal.get("preference_key") or "")
    prefix = key.split(".", 1)[0]
    return {
        "proposal_id": proposal.get("proposal_id"),
        "preference_key": key,
        "status": proposal.get("status"),
        "proposal_type": proposal.get("proposal_type"),
        "reason": proposal.get("reason"),
        "confidence": proposal.get("confidence"),
        "evidence_count": proposal.get("evidence_count"),
        "application_scope": REVIEW_ONLY_PREFIXES.get(prefix, "review_only"),
        "auto_apply_allowed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review Rocky assistant learning calibration state.")
    parser.add_argument("--learning-db", default=str(DEFAULT_LEARNING_DB_PATH), dest="learning_db")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_assistant_learning_calibration_review(learning_db_path=args.learning_db, limit=args.limit)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else f"Assistant learning calibration: {payload.get('status')}")
    return 0 if payload.get("status") in {"ok", "calibration_pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
