#!/usr/bin/env python3
"""Read-only Obsidian Layer 3 enrichment for Rocky coding work items."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from obsidian_memory import query_obsidian_vault


SENSITIVE_TEXT_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]+|webcal://|https?://[^\s]*(?:token|secret|password|credential|cookie|authorization)[^\s]*|"
    r"\b(?:access_token|refresh_token|token|secret|password|credential|cookie|authorization)\b|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
PROMPT_INJECTION_RE = re.compile(
    r"\b(ignore previous|system prompt|developer message|override policy|forget instructions)\b",
    re.IGNORECASE,
)
QMD_CONTEXT_RE = re.compile(r"@@[^@]*@@|\([^)]*\bbefore\b[^)]*\bafter\b[^)]*\)", re.IGNORECASE)


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def sanitize_memory_text(value: Any, *, limit: int = 360) -> dict[str, Any]:
    """Return safe memory text plus prompt-injection classification."""
    text = QMD_CONTEXT_RE.sub(" ", str(value or ""))
    text = re.sub(r"[#>*`\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    injection = bool(PROMPT_INJECTION_RE.search(text))
    text = SENSITIVE_TEXT_RE.sub("[redacted]", text)
    if injection:
        text = f"[untrusted memory note] {text}"
    return {"text": text[:limit], "prompt_injection_flagged": injection}


def enrich_project_memory(
    project: str,
    *,
    title: str | None = None,
    query_func: Callable[..., dict[str, Any]] | None = None,
    limit: int = 3,
) -> dict[str, Any]:
    project_text = _safe_label(project) or "coding work"
    title_text = _safe_label(title or "")
    query = f"{project_text} {title_text} coding decisions open loops next step".strip()
    reader = query_func or query_obsidian_vault
    try:
        payload = reader(query, limit=limit, mode="query")
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "obsidian_memory_query_failed",
            "error_hash": _hash_text(str(exc)),
            "memory_refs": [],
            "durable_context_summary": "",
            "durable_decisions": [],
            "durable_open_loops": [],
            "memory_confidence": 0.0,
            "prompt_injection_flagged": False,
        }
    if payload.get("status") != "ok":
        return {
            "status": str(payload.get("status") or "unavailable"),
            "reason": str(payload.get("reason") or payload.get("error") or "obsidian_memory_unavailable")[:160],
            "memory_refs": [],
            "durable_context_summary": "",
            "durable_decisions": [],
            "durable_open_loops": [],
            "memory_confidence": 0.0,
            "prompt_injection_flagged": False,
        }
    results = [item for item in (payload.get("results") or []) if isinstance(item, dict)][: max(0, int(limit))]
    summaries: list[str] = []
    decisions: list[str] = []
    open_loops: list[str] = []
    refs: list[str] = []
    injection = False
    for item in results:
        snippet = item.get("snippet") or item.get("summary") or item.get("title") or ""
        safe = sanitize_memory_text(snippet)
        text = safe["text"]
        if not text:
            continue
        injection = injection or bool(safe.get("prompt_injection_flagged"))
        summaries.append(text)
        lowered = text.lower()
        if any(marker in lowered for marker in ("decision", "decisions", "approved", "policy", "codex_managed")):
            decisions.append(text[:220])
        if any(marker in lowered for marker in ("open loop", "open loops", "next", "sprint", "todo", "to do", "follow up")):
            open_loops.append(text[:220])
        ref = _memory_ref(item)
        if ref and ref not in refs:
            refs.append(ref)
    if not summaries:
        return {
            "status": "empty",
            "reason": "no_relevant_obsidian_memory",
            "memory_refs": refs,
            "durable_context_summary": "",
            "durable_decisions": [],
            "durable_open_loops": [],
            "memory_confidence": 0.0,
            "prompt_injection_flagged": injection,
        }
    return {
        "status": "ok",
        "query_hash": _hash_text(query),
        "result_count": len(results),
        "memory_refs": refs[:5],
        "durable_context_summary": " ".join(summaries[:2])[:420],
        "durable_decisions": _unique(decisions)[:3],
        "durable_open_loops": _unique(open_loops)[:3],
        "memory_confidence": 0.72 if refs else 0.55,
        "prompt_injection_flagged": injection,
    }


def enrich_coding_work_items(
    work_items: list[dict[str, Any]],
    *,
    enabled: bool = True,
    query_func: Callable[..., dict[str, Any]] | None = None,
    max_projects: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = [dict(item) for item in work_items]
    if not enabled:
        return items, {"status": "skipped", "reason": "memory_disabled", "queried_projects": 0}
    if not items:
        return items, {"status": "skipped", "reason": "no_work_items", "queried_projects": 0}
    queried = 0
    ok = 0
    failed = 0
    for item in items[: max(0, int(max_projects))]:
        project = str(item.get("project") or "").strip()
        if not project:
            continue
        queried += 1
        memory = enrich_project_memory(project, title=str(item.get("title") or ""), query_func=query_func)
        item["memory_status"] = memory.get("status")
        item["memory_refs"] = memory.get("memory_refs") or []
        item["durable_context_summary"] = memory.get("durable_context_summary") or ""
        item["durable_decisions"] = memory.get("durable_decisions") or []
        item["durable_open_loops"] = memory.get("durable_open_loops") or []
        item["memory_confidence"] = memory.get("memory_confidence") or 0.0
        if memory.get("prompt_injection_flagged"):
            item["prompt_injection_flagged"] = True
            item["requires_dusan_decision"] = True
        if memory.get("status") == "ok":
            ok += 1
        elif memory.get("status") not in {"empty", "skipped"}:
            failed += 1
    status = "ok" if ok else "empty" if queried and not failed else "failed" if failed else "skipped"
    return items, {"status": status, "queried_projects": queried, "enriched_projects": ok, "failed_projects": failed}


def _safe_label(value: Any) -> str:
    safe = sanitize_memory_text(value, limit=120)
    return safe["text"].replace("[untrusted memory note]", "").strip()


def _memory_ref(item: dict[str, Any]) -> str:
    path = str(item.get("path") or "").strip()
    if path:
        return f"obsidian:{path[:220]}"
    title = str(item.get("title") or "").strip()
    return f"obsidian:title:{_hash_text(title)}" if title else ""


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result
