#!/usr/bin/env python3
"""Resolve Rocky task candidates against existing Notion task identity."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

from notion_task_manager import (
    TERMINAL_TASK_STATUSES,
    legacy_source_dedupe_key,
    stable_task_action_fingerprint,
    stable_task_dedupe_key,
    stable_task_id,
)

POLICY_VERSION = "rocky-task-identity-v1"
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "should", "the", "to", "with", "whether", "dusan", "task",
}
LEGACY_MATCH_THRESHOLD = 0.18
LEGACY_MATCH_MARGIN = 0.08


def resolve_task_identities(
    candidates: list[dict[str, Any]],
    *,
    existing_tasks: list[dict[str, Any]] | None = None,
    today: str | date | None = None,
) -> dict[str, Any]:
    day = today.isoformat() if isinstance(today, date) else str(today or date.today().isoformat())
    existing = list(existing_tasks or [])
    prepared = [_prepare_candidate(candidate, day) for candidate in candidates]
    legacy_assignments = _assign_legacy_source_matches(prepared, existing)
    assigned_legacy_sources = {str(task.get("source_ref") or "") for task in legacy_assignments.values()}

    by_dedupe = {str(task.get("dedupe_key") or ""): task for task in existing if task.get("dedupe_key")}
    by_action = {str(task.get("action_fingerprint") or ""): task for task in existing if task.get("action_fingerprint")}

    results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for idx, task in enumerate(prepared):
        dedupe_key = str(task.get("dedupe_key") or "")
        action_fingerprint = str(task.get("action_fingerprint") or "")
        if dedupe_key in seen_keys:
            results.append(_result("duplicate", "duplicate_candidate_in_batch", task))
            continue
        seen_keys.add(dedupe_key)

        existing_match = by_dedupe.get(dedupe_key) or by_action.get(action_fingerprint)
        migration = None
        if not existing_match and idx in legacy_assignments:
            existing_match = legacy_assignments[idx]
            migration = "legacy_source_ref_to_action_identity"
        if existing_match:
            if _is_terminal(existing_match):
                results.append(_result("terminal_match_skipped", "existing_task_terminal", task, existing_match, migration))
            else:
                merged_task = _with_existing_update_state(task, existing_match, day, migration)
                results.append(_result("update", "existing_task_matched", merged_task, existing_match, migration))
            continue

        legacy_candidates = _legacy_candidates_for_source(task, existing)
        if legacy_candidates and str(task.get("source_ref") or "") not in assigned_legacy_sources:
            results.append(_result("manual_review_required", "legacy_source_ref_action_mismatch", task, legacy_candidates[0]))
            continue

        results.append(_result("create", "new_task_identity", task))

    counts = {name: sum(1 for item in results if item["action"] == name) for name in ["create", "update", "duplicate", "terminal_match_skipped", "manual_review_required"]}
    return {
        "status": "ok",
        "policy_version": POLICY_VERSION,
        "resolved_count": len(results),
        "results": results,
        "create_count": counts["create"],
        "update_count": counts["update"],
        "duplicate_count": counts["duplicate"],
        "terminal_skipped_count": counts["terminal_match_skipped"],
        "manual_review_count": counts["manual_review_required"],
        "migrated_count": sum(1 for item in results if item.get("migration")),
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _prepare_candidate(candidate: dict[str, Any], day: str) -> dict[str, Any]:
    task = dict(candidate)
    task["action_fingerprint"] = str(task.get("action_fingerprint") or stable_task_action_fingerprint(task))
    task["dedupe_key"] = str(task.get("dedupe_key") or stable_task_dedupe_key(task))
    task["rocky_task_id"] = str(task.get("rocky_task_id") or stable_task_id(task))
    task["last_detected_date"] = str(task.get("last_detected_date") or day)
    task["detection_count"] = max(1, int(task.get("detection_count") or 1))
    return task


def _result(action: str, reason: str, task: dict[str, Any], existing: dict[str, Any] | None = None, migration: str | None = None) -> dict[str, Any]:
    item = {
        "action": action,
        "reason": reason,
        "task": task,
        "dedupe_key": task.get("dedupe_key"),
        "action_fingerprint": task.get("action_fingerprint"),
    }
    if existing:
        item["existing_page_id"] = existing.get("page_id") or existing.get("id")
        item["existing_status"] = existing.get("status")
    if migration:
        item["migration"] = migration
    return item


def _with_existing_update_state(task: dict[str, Any], existing: dict[str, Any], day: str, migration: str | None) -> dict[str, Any]:
    updated = dict(task)
    updated["existing_page_id"] = existing.get("page_id") or existing.get("id")
    updated["page_id"] = existing.get("page_id") or existing.get("id")
    updated["detection_count"] = int(existing.get("detection_count") or 0) + 1
    updated["last_detected_date"] = day
    updated["last_lifecycle_reason"] = migration or "existing_task_detected"
    return updated


def _assign_legacy_source_matches(candidates: list[dict[str, Any]], existing: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    assignments: dict[int, dict[str, Any]] = {}
    for legacy in existing:
        if not _is_legacy_source_task(legacy):
            continue
        same_source = [(idx, candidate) for idx, candidate in enumerate(candidates) if candidate.get("source_ref") == legacy.get("source_ref")]
        if not same_source:
            continue
        scored = sorted(((_text_similarity(candidate, legacy), idx, candidate) for idx, candidate in same_source), reverse=True)
        best_score, best_idx, _ = scored[0]
        next_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= LEGACY_MATCH_THRESHOLD and best_score - next_score >= LEGACY_MATCH_MARGIN:
            assignments[best_idx] = legacy
    return assignments


def _legacy_candidates_for_source(task: dict[str, Any], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in existing if _is_legacy_source_task(item) and item.get("source_ref") == task.get("source_ref")]


def _is_legacy_source_task(task: dict[str, Any]) -> bool:
    source_key = legacy_source_dedupe_key(task)
    dedupe_key = str(task.get("dedupe_key") or "")
    return bool(source_key and dedupe_key == source_key and not task.get("action_fingerprint"))


def _is_terminal(task: dict[str, Any]) -> bool:
    return str(task.get("status") or "").title() in TERMINAL_TASK_STATUSES


def _text_similarity(candidate: dict[str, Any], existing: dict[str, Any]) -> float:
    left = _tokens(candidate)
    right = _tokens(existing)
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _tokens(task: dict[str, Any]) -> set[str]:
    text = " ".join(str(task.get(key) or "") for key in ["title", "description", "related_project", "related_person_company"])
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2 and token not in STOPWORDS}


def hash_task_identity(task: dict[str, Any]) -> str:
    payload = json.dumps({"dedupe_key": task.get("dedupe_key"), "action_fingerprint": task.get("action_fingerprint")}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
