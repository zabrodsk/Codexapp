#!/usr/bin/env python3
"""Sanitized git repository signal inspector for Rocky coding work."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_signal_sync import inspect_repositories, utc_now_iso


DEFAULT_REMOTE_REPO_ROOTS = [
    "/Users/clawdbot/.openclaw/workspace",
    "/Users/clawdbot/apps",
    "/Users/clawdbot/Rockaway-Deal-Intelligence",
    "/Users/clawdbot/rockaway-leadgen",
]


def build_repo_signal_summary(*, repo_roots: list[str | Path] | None = None, limit: int = 80) -> dict[str, Any]:
    repos = inspect_repositories(repo_roots=repo_roots or DEFAULT_REMOTE_REPO_ROOTS, limit=limit)
    active = [repo for repo in repos if repo.get("dirty")]
    return {
        "status": "ok",
        "observed_at": utc_now_iso(),
        "repo_count": len(repos),
        "dirty_repo_count": len(active),
        "repos": repos,
        "calendar_write_attempted": False,
    }
