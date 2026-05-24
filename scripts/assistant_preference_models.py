#!/usr/bin/env python3
"""Bounded preference model updates for Rocky assistant learning."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from assistant_learning_store import AssistantLearningStore, DEFAULT_LEARNING_DB_PATH, read_active_bounded_preference, sanitize_payload

EMAIL_DURATION_KEY = "email_triage.duration_multiplier"
CODING_DURATION_KEY = "coding_focus.default_duration_minutes"
MIN_EVIDENCE = 5
MIN_CONFIDENCE = 0.70
EMAIL_BOUNDS = {"min": 0.75, "max": 1.25}
CODING_BOUNDS = {"min": 60, "max": 120}


def update_preference_models(*, db_path: str | Path | None = None, outcomes: list[dict[str, Any]] | None = None, live: bool = False) -> dict[str, Any]:
    store = AssistantLearningStore(db_path) if (live or outcomes is None) else None
    rows = outcomes if outcomes is not None else (store.list_outcomes(limit=1000) if store else [])
    model_payloads = [_email_duration_model(rows), _coding_duration_model(rows)]
    proposal_payloads = _proposal_only_models(rows)
    written_models: list[dict[str, Any]] = []
    written_proposals: list[dict[str, Any]] = []
    if live:
        assert store is not None
        for model in model_payloads:
            written_models.append(store.upsert_preference_model(model))
        for proposal in proposal_payloads:
            written_proposals.append(store.create_learning_proposal(proposal))
    active = [model for model in (written_models if live else model_payloads) if model.get("status") == "active_bounded"]
    status = "ok" if active else "skipped_insufficient_evidence"
    return sanitize_payload({
        "status": status,
        "reason": "preference_models_updated" if live else "preference_models_preview",
        "live": bool(live),
        "model_count": len(model_payloads),
        "active_bounded_count": len(active),
        "proposal_count": len(proposal_payloads),
        "models": written_models if live else model_payloads,
        "proposals": written_proposals if live else proposal_payloads,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    })


def learning_summary(*, db_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(db_path) if db_path else DEFAULT_LEARNING_DB_PATH
    if not path.exists():
        return {"status": "empty", "reason": "assistant_learning_store_missing", "active_bounded_count": 0, "proposal_count": 0, "outcome_count": 0, "preferences": [], "proposals": []}
    store = AssistantLearningStore(path)
    models = store.list_preference_models()
    proposals = store.list_learning_proposals(status="proposed", limit=20)
    outcomes = store.list_outcomes(limit=20)
    return sanitize_payload({
        "status": "ok",
        "active_bounded_count": sum(1 for model in models if model.get("status") == "active_bounded"),
        "proposal_count": len(proposals),
        "outcome_count": len(outcomes),
        "preferences": models,
        "proposals": proposals,
        "latest_outcome": outcomes[0] if outcomes else None,
    })


def active_email_duration_multiplier(*, db_path: str | Path | None = None, preferences: dict[str, Any] | None = None) -> dict[str, Any] | None:
    model = _preference_from_mapping(preferences, EMAIL_DURATION_KEY) if preferences else read_active_bounded_preference(EMAIL_DURATION_KEY, db_path=db_path)
    if not model:
        return None
    value = _clamp(float(model.get("value") or 1.0), EMAIL_BOUNDS["min"], EMAIL_BOUNDS["max"])
    return {"preference_key": EMAIL_DURATION_KEY, "value": value, "confidence": float(model.get("confidence") or 0), "status": model.get("status")}


def active_coding_default_duration(*, db_path: str | Path | None = None, preferences: dict[str, Any] | None = None) -> dict[str, Any] | None:
    model = _preference_from_mapping(preferences, CODING_DURATION_KEY) if preferences else read_active_bounded_preference(CODING_DURATION_KEY, db_path=db_path)
    if not model:
        return None
    value = int(round(_clamp(float(model.get("value") or 90), CODING_BOUNDS["min"], CODING_BOUNDS["max"]) / 15) * 15)
    return {"preference_key": CODING_DURATION_KEY, "value": value, "confidence": float(model.get("confidence") or 0), "status": model.get("status")}


def _email_duration_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ratios = []
    for row in rows:
        if row.get("lane") != "email_triage":
            continue
        predicted = _num(row.get("predicted_minutes") or row.get("booked_minutes"))
        actual = _num(row.get("actual_minutes"))
        if predicted and actual and predicted > 0:
            ratios.append(actual / predicted)
    evidence = len(ratios)
    confidence = _confidence(evidence)
    raw = statistics.median(ratios) if ratios else 1.0
    value = _clamp(raw, EMAIL_BOUNDS["min"], EMAIL_BOUNDS["max"])
    status = "active_bounded" if evidence >= MIN_EVIDENCE and confidence >= MIN_CONFIDENCE else "insufficient_evidence"
    return {"preference_key": EMAIL_DURATION_KEY, "lane": "email_triage", "status": status, "value": value, "confidence": confidence, "evidence_count": evidence, "bounds": EMAIL_BOUNDS, "model": {"raw_median_multiplier": raw, "sample_count": evidence}, "reason": "bounded_email_duration_multiplier" if status == "active_bounded" else "insufficient_email_duration_evidence"}


def _coding_duration_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = []
    for row in rows:
        if row.get("lane") != "coding_focus":
            continue
        actual = _num(row.get("actual_minutes") or row.get("booked_minutes"))
        if actual and actual >= 30:
            durations.append(actual)
    evidence = len(durations)
    confidence = _confidence(evidence)
    raw = statistics.median(durations) if durations else 90
    value = int(round(_clamp(raw, CODING_BOUNDS["min"], CODING_BOUNDS["max"]) / 15) * 15)
    status = "active_bounded" if evidence >= MIN_EVIDENCE and confidence >= MIN_CONFIDENCE else "insufficient_evidence"
    return {"preference_key": CODING_DURATION_KEY, "lane": "coding_focus", "status": status, "value": value, "confidence": confidence, "evidence_count": evidence, "bounds": CODING_BOUNDS, "model": {"raw_median_minutes": raw, "sample_count": evidence}, "reason": "bounded_coding_focus_default_duration" if status == "active_bounded" else "insufficient_coding_duration_evidence"}


def _proposal_only_models(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes = {str(row.get("lane") or "") for row in rows}
    proposals: list[dict[str, Any]] = []
    if "training" in lanes:
        proposals.append(_proposal("training.preferred_start_time", "training_timing_observed_review_required", rows))
    if "task_reminder" in lanes:
        proposals.append(_proposal("task_reminder.cadence_tolerance", "task_reminder_cadence_observed_review_required", rows))
    if "task_spine" in lanes:
        proposals.append(_proposal("task_completion.patterns", "task_completion_pattern_review_required", rows))
    if "daily_briefing" in lanes:
        proposals.append(_proposal("daily_priority.weights", "daily_priority_weight_review_required", rows))
    return proposals


def _proposal(key: str, reason: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    root = key.split(".", 1)[0]
    evidence = sum(1 for row in rows if root in str(row.get("lane") or ""))
    return {"preference_key": key, "proposal_type": "review_required", "status": "proposed", "reason": reason, "confidence": min(0.65, _confidence(evidence)), "evidence_count": evidence, "proposal": {"auto_apply_allowed": False, "safety": "review_only"}}


def _preference_from_mapping(preferences: dict[str, Any], key: str) -> dict[str, Any] | None:
    if key in preferences and isinstance(preferences[key], dict):
        return preferences[key]
    for model in preferences.get("preferences", []) if isinstance(preferences.get("preferences"), list) else []:
        if model.get("preference_key") == key:
            return model
    return None


def _confidence(evidence: int) -> float:
    return min(0.95, round(0.5 + max(0, evidence) * 0.05, 2))


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or update Rocky assistant preference models.")
    parser.add_argument("--db-path", default=str(DEFAULT_LEARNING_DB_PATH), dest="db_path")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = update_preference_models(db_path=args.db_path, live=args.live)
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json_output else f"Preference models: {payload.get('status')}")
    return 0 if payload.get("status") in {"ok", "skipped_insufficient_evidence"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
