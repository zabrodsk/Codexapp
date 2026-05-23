#!/usr/bin/env python3
"""Build sanitized coding work signals from Codex, Claude, and repo metadata."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_repo_inspector import build_repo_signal_summary
from coding_signal_sync import (
    DEFAULT_MAX_SESSIONS,
    inspect_claude_sessions,
    inspect_codex_sessions,
    project_from_path,
    sanitize_text,
    safe_path_ref,
    utc_now_iso,
)


DEFAULT_LAPTOP_MANIFEST_PATH = Path("/Users/clawdbot/.openclaw/inbox/coding-signals/dusan-laptop/latest.json")


def load_laptop_manifest(path: str | Path | None = None) -> dict[str, Any] | None:
    path = DEFAULT_LAPTOP_MANIFEST_PATH if path is None else path
    if not path:
        return None
    target = Path(path).expanduser()
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def inspect_coding_signals(
    *,
    laptop_manifest_path: str | Path | None = None,
    include_local_sessions: bool = True,
    include_repos: bool = True,
    repo_roots: list[str | Path] | None = None,
    limit: int = DEFAULT_MAX_SESSIONS,
) -> dict[str, Any]:
    manifest = load_laptop_manifest(laptop_manifest_path)
    signals: list[dict[str, Any]] = []
    if manifest:
        signals.extend(_signals_from_manifest(manifest, origin="laptop_manifest"))
    if include_local_sessions:
        local_manifest = {
            "generated_at": utc_now_iso(),
            "codex_sessions": inspect_codex_sessions(limit=limit),
            "claude_sessions": inspect_claude_sessions(limit=limit),
            "repos": [],
        }
        signals.extend(_signals_from_manifest(local_manifest, origin="mac_mini_local"))
    if include_repos:
        try:
            repo_summary = build_repo_signal_summary(repo_roots=repo_roots)
        except Exception as exc:
            repo_summary = {"status": "degraded", "reason": "repo_inspection_failed", "error_hash": _hash_text(str(exc)), "repos": [], "repo_count": 0, "dirty_repo_count": 0}
    else:
        repo_summary = {"status": "skipped", "reason": "repo_inspection_disabled", "repos": [], "repo_count": 0, "dirty_repo_count": 0}
    for repo in repo_summary.get("repos") or []:
        signals.append(_repo_signal(repo))
    return {
        "status": "ok" if signals else "empty",
        "observed_at": utc_now_iso(),
        "laptop_manifest_status": _manifest_status(manifest),
        "signal_count": len(signals),
        "signals": signals,
        "repo_summary": {"status": repo_summary.get("status"), "repo_count": repo_summary.get("repo_count"), "dirty_repo_count": repo_summary.get("dirty_repo_count"), "reason": repo_summary.get("reason")},
        "repo_visibility": {"status": repo_summary.get("status"), "repo_count": repo_summary.get("repo_count", 0), "dirty_repo_count": repo_summary.get("dirty_repo_count", 0)},
        "calendar_write_attempted": False,
    }


def _manifest_status(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not manifest:
        return {"status": "missing"}
    generated_at = str(manifest.get("generated_at") or "")
    stale = False
    try:
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        stale = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() > 6 * 3600
    except Exception:
        stale = True
    return {"status": "stale" if stale else "ok", "generated_at": generated_at}


def _signals_from_manifest(manifest: dict[str, Any], *, origin: str) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for session in manifest.get("codex_sessions") or []:
        signals.append(_session_signal(session, provider="codex", origin=origin))
    for session in manifest.get("claude_sessions") or []:
        signals.append(_session_signal(session, provider="claude", origin=origin))
    for repo in manifest.get("repos") or []:
        signals.append(_repo_signal(repo, origin=origin))
    return signals


def _session_signal(session: dict[str, Any], *, provider: str, origin: str) -> dict[str, Any]:
    project = sanitize_text(session.get("project") or project_from_path(session.get("cwd")) or provider, limit=80)
    thread = sanitize_text(session.get("thread_name") or "", limit=140)
    branch = sanitize_text(session.get("git_branch") or session.get("branch") or "", limit=80)
    title = thread or f"Resume {project} work from {provider}"
    where = f"Last {provider} session for {project}"
    if branch:
        where += f" on branch {branch}"
    return {
        "signal_id": f"coding-signal:{provider}:{session.get('session_id')}",
        "source": provider,
        "origin": origin,
        "source_ref": session.get("source_ref") or f"{provider}:session:{session.get('session_id')}",
        "project": project,
        "title": sanitize_text(title, limit=160),
        "summary": sanitize_text(thread or where, limit=220),
        "where_left_off": sanitize_text(where, limit=220),
        "recommended_next_step": "Open the referenced session/repo and continue the last unfinished implementation thread.",
        "cwd": safe_path_ref(session.get("cwd")),
        "branch": branch,
        "last_seen_at": session.get("updated_at") or session.get("last_seen_at") or "",
        "confidence_hint": 0.76,
        "signal_kind": "session",
        "fresh_signal": sanitize_text(thread or where, limit=220),
        "prompt_injection_flagged": bool(session.get("prompt_injection_flagged")),
        "evidence_refs": [session.get("source_ref") or f"{provider}:session:{session.get('session_id')}"]
    }


def _repo_signal(repo: dict[str, Any], *, origin: str = "repo_inspector") -> dict[str, Any]:
    project = sanitize_text(repo.get("project") or project_from_path(repo.get("path")) or "repository", limit=80)
    dirty = bool(repo.get("dirty"))
    branch = sanitize_text(repo.get("branch") or "", limit=80)
    modified = int(repo.get("modified_count") or 0)
    where = f"Repository {project}"
    if branch:
        where += f" on branch {branch}"
    if dirty:
        where += f" has {modified} changed file(s)"
    return {
        "signal_id": f"coding-signal:repo:{repo.get('source_ref')}",
        "source": "git_repo",
        "origin": origin,
        "source_ref": repo.get("source_ref") or f"git:repo:{project}",
        "project": project,
        "title": f"Continue {project}" if dirty else f"Review {project}",
        "summary": where,
        "where_left_off": where,
        "recommended_next_step": "Review working tree status and finish or park the active changes." if dirty else "Check whether this repository still has an active next step.",
        "cwd": safe_path_ref(repo.get("path")),
        "branch": branch,
        "last_seen_at": repo.get("last_seen_at") or utc_now_iso(),
        "confidence_hint": 0.55 if dirty else 0.35,
        "signal_kind": "repo",
        "fresh_signal": where,
        "prompt_injection_flagged": False,
        "evidence_refs": [repo.get("source_ref") or f"git:repo:{project}"],
        "dirty": dirty,
        "modified_count": modified,
    }


def _hash_text(value: Any) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]
