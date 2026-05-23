#!/usr/bin/env python3
"""Task candidate dedupe helpers for Rocky."""
from __future__ import annotations

from typing import Any

from notion_task_manager import stable_task_dedupe_key


def dedupe_task_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("dedupe_key") or stable_task_dedupe_key(candidate))
        candidate = {**candidate, "dedupe_key": key}
        if key in by_key:
            duplicates.append({"dedupe_key": key, "title": candidate.get("title")})
            by_key[key] = _merge_candidate(by_key[key], candidate)
        else:
            by_key[key] = candidate
    return {
        "status": "ok",
        "candidates": list(by_key.values()),
        "candidate_count": len(by_key),
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "calendar_write_attempted": False,
        "notion_write_attempted": False,
    }


def _merge_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if float(incoming.get("confidence") or 0) > float(existing.get("confidence") or 0):
        merged = {**existing, **incoming}
    else:
        merged = dict(existing)
    refs = [existing.get("source_ref"), incoming.get("source_ref")]
    merged["source_ref"] = ",".join(sorted({str(ref) for ref in refs if ref}))
    merged["evidence_hash"] = existing.get("evidence_hash") or incoming.get("evidence_hash")
    return merged
