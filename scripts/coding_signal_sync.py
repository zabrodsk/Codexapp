#!/usr/bin/env python3
"""Sanitized laptop coding signal collector and sync helper."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "rocky-coding-signals-v1"
DEFAULT_REMOTE_HOST = os.getenv("ROCKY_CODING_SIGNAL_REMOTE_HOST", "clawdbot-mini")
DEFAULT_REMOTE_PATH = Path("/Users/clawdbot/.openclaw/inbox/coding-signals/dusan-laptop/latest.json")
DEFAULT_MAX_SESSIONS = 30
DEFAULT_REPO_ROOTS = [
    Path("/Users/dusan.zabrodsky/Library/CloudStorage/OneDrive-Personal/Rockaway/Ventures"),
    Path("/Users/dusan.zabrodsky/code"),
    Path("/Users/clawdbot"),
    Path("/Users/clawdbot/apps"),
    Path("/Users/clawdbot/.openclaw/workspace"),
]

SENSITIVE_TEXT_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]+|webcal://|https?://[^\s]*(?:token|secret|password|credential|auth|cookie)[^\s]*|"
    r"\b(?:token|secret|password|credential|cookie|authorization|access_token|refresh_token)\b|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
PROMPT_INJECTION_RE = re.compile(r"\b(ignore previous|system prompt|developer message|override policy|forget instructions)\b", re.IGNORECASE)


def hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_text(value: Any, *, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if SENSITIVE_TEXT_RE.search(text):
        return f"[redacted:{hash_text(text)}]"
    return text[:limit]


def safe_path_ref(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if SENSITIVE_TEXT_RE.search(text):
        return f"path:{hash_text(text)}"
    return text[:240]


def _jsonl_rows(path: Path, *, max_rows: int = 2000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(rows) >= max_rows:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def _latest_files(root: Path, *, pattern: str, limit: int) -> list[Path]:
    if not root.exists():
        return []
    files = [path for path in root.rglob(pattern) if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[: max(0, int(limit))]


def inspect_codex_sessions(
    *,
    sessions_root: str | Path | None = None,
    index_path: str | Path | None = None,
    limit: int = DEFAULT_MAX_SESSIONS,
) -> list[dict[str, Any]]:
    root = Path(sessions_root).expanduser() if sessions_root else Path.home() / ".codex/sessions"
    index = _load_codex_index(Path(index_path).expanduser() if index_path else Path.home() / ".codex/session_index.jsonl")
    sessions: list[dict[str, Any]] = []
    for path in _latest_files(root, pattern="rollout-*.jsonl", limit=limit):
        rows = _jsonl_rows(path, max_rows=400)
        meta = next((row.get("payload") for row in rows if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict)), {}) or {}
        session_id = str(meta.get("id") or path.stem.split("-")[-1])
        turn = next((row.get("payload") for row in reversed(rows) if row.get("type") == "turn_context" and isinstance(row.get("payload"), dict)), {}) or {}
        cwd = str(turn.get("cwd") or meta.get("cwd") or "")
        updated_at = str(index.get(session_id, {}).get("updated_at") or meta.get("timestamp") or "")
        thread_name = str(index.get(session_id, {}).get("thread_name") or "")
        sessions.append(
            {
                "provider": "codex",
                "session_id": session_id,
                "source_ref": f"codex:session:{session_id}",
                "cwd": safe_path_ref(cwd),
                "project": project_from_path(cwd) or sanitize_text(thread_name, limit=80) or "Codex",
                "thread_name": sanitize_text(thread_name, limit=140),
                "updated_at": updated_at,
                "file_ref": safe_path_ref(path),
                "message_count": sum(1 for row in rows if row.get("type") == "response_item"),
                "content_hash": hash_text(path),
                "prompt_injection_flagged": bool(PROMPT_INJECTION_RE.search(thread_name)),
            }
        )
    return sessions


def _load_codex_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in _jsonl_rows(path, max_rows=5000):
        item_id = str(row.get("id") or "")
        if item_id:
            index[item_id] = row
    return index


def inspect_claude_sessions(*, projects_root: str | Path | None = None, limit: int = DEFAULT_MAX_SESSIONS) -> list[dict[str, Any]]:
    root = Path(projects_root).expanduser() if projects_root else Path.home() / ".claude/projects"
    sessions: list[dict[str, Any]] = []
    for path in _latest_files(root, pattern="*.jsonl", limit=limit):
        rows = _jsonl_rows(path, max_rows=600)
        session_id = ""
        cwd = ""
        branch = ""
        last_seen = ""
        for row in rows:
            session_id = str(row.get("sessionId") or session_id)
            cwd = str(row.get("cwd") or cwd)
            branch = str(row.get("gitBranch") or branch)
            last_seen = str(row.get("timestamp") or last_seen)
        if not session_id:
            session_id = path.stem
        sessions.append(
            {
                "provider": "claude",
                "session_id": session_id,
                "source_ref": f"claude:session:{session_id}",
                "cwd": safe_path_ref(cwd),
                "project": project_from_path(cwd) or path.parent.name[:80],
                "git_branch": sanitize_text(branch, limit=80),
                "updated_at": last_seen,
                "file_ref": safe_path_ref(path),
                "message_count": len(rows),
                "content_hash": hash_text(path),
                "prompt_injection_flagged": False,
            }
        )
    return sessions


def inspect_repositories(*, repo_roots: list[str | Path] | None = None, limit: int = 80) -> list[dict[str, Any]]:
    roots = [Path(item).expanduser() for item in (repo_roots or DEFAULT_REPO_ROOTS) if Path(item).expanduser().exists()]
    repos: list[Path] = []
    for root in roots:
        if (root / ".git").exists():
            repos.append(root)
            continue
        for git_dir in root.rglob(".git"):
            if len(repos) >= limit:
                break
            if git_dir.is_dir():
                repos.append(git_dir.parent)
        if len(repos) >= limit:
            break
    unique: list[Path] = []
    seen: set[str] = set()
    for repo in repos:
        key = str(repo.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(repo)
    return [_inspect_repo(repo) for repo in unique[:limit]]


def _inspect_repo(repo: Path) -> dict[str, Any]:
    status = _git(repo, ["status", "--porcelain=v1", "-uno"])
    branch = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _git(repo, ["rev-parse", "--short", "HEAD"])
    remote = _git(repo, ["remote", "get-url", "origin"])
    changed_lines = [line for line in status.splitlines() if line.strip()]
    return {
        "source_ref": f"git:repo:{hash_text(repo.resolve())}",
        "path": safe_path_ref(repo.resolve()),
        "project": project_from_path(repo) or repo.name,
        "branch": sanitize_text(branch, limit=80),
        "head": sanitize_text(commit, limit=40),
        "remote_hash": hash_text(remote) if remote else None,
        "dirty": bool(changed_lines),
        "modified_count": sum(1 for line in changed_lines if line[:2].strip()),
        "last_seen_at": utc_now_iso(),
    }


def _git(repo: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=8, check=False)
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return sanitize_text(proc.stdout.strip(), limit=240)


def project_from_path(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    path = Path(text)
    name = path.name or path.parent.name
    if name == ".git":
        name = path.parent.name
    return sanitize_text(name.replace("--claude-worktrees-", " worktree "), limit=80)


def build_coding_signal_manifest(
    *,
    codex_sessions_root: str | Path | None = None,
    codex_index_path: str | Path | None = None,
    claude_projects_root: str | Path | None = None,
    repo_roots: list[str | Path] | None = None,
    limit: int = DEFAULT_MAX_SESSIONS,
) -> dict[str, Any]:
    codex = inspect_codex_sessions(sessions_root=codex_sessions_root, index_path=codex_index_path, limit=limit)
    claude = inspect_claude_sessions(projects_root=claude_projects_root, limit=limit)
    repos = inspect_repositories(repo_roots=repo_roots, limit=80)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "host": sanitize_text(socket.gethostname(), limit=80),
        "platform": sanitize_text(platform.platform(), limit=120),
        "privacy": {
            "sanitized": True,
            "raw_transcripts_copied": False,
            "diffs_copied": False,
            "secrets_copied": False,
        },
        "counts": {"codex_sessions": len(codex), "claude_sessions": len(claude), "repos": len(repos)},
        "codex_sessions": codex,
        "claude_sessions": claude,
        "repos": repos,
    }


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(redact_payload(manifest), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"status": "ok", "path": str(target), "sha256": hash_text(target.read_text(encoding="utf-8")), "bytes": target.stat().st_size}


def push_manifest_to_remote(manifest: dict[str, Any], *, remote_host: str = DEFAULT_REMOTE_HOST, remote_path: str | Path = DEFAULT_REMOTE_PATH) -> dict[str, Any]:
    remote = str(remote_path)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(json.dumps(redact_payload(manifest), indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    try:
        mkdir = subprocess.run(["ssh", remote_host, "mkdir", "-p", str(Path(remote).parent)], capture_output=True, text=True, timeout=20, check=False)
        if mkdir.returncode != 0:
            return {"status": "blocked", "reason": "remote_mkdir_failed", "stderr_hash": hash_text(mkdir.stderr), "calendar_write_attempted": False}
        proc = subprocess.run(["scp", str(tmp_path), f"{remote_host}:{remote}"], capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            return {"status": "blocked", "reason": "remote_scp_failed", "stderr_hash": hash_text(proc.stderr), "calendar_write_attempted": False}
        return {"status": "ok", "remote_host": remote_host, "remote_path": remote, "sha256": hash_text(tmp_path.read_text(encoding="utf-8")), "calendar_write_attempted": False}
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        return f"[redacted:{hash_text(value)}]"
    return value


def run_sync(
    *,
    output_path: str | Path | None = None,
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_path: str | Path = DEFAULT_REMOTE_PATH,
    push: bool = True,
    limit: int = DEFAULT_MAX_SESSIONS,
) -> dict[str, Any]:
    manifest = build_coding_signal_manifest(limit=limit)
    local = write_manifest(output_path, manifest) if output_path else None
    pushed = push_manifest_to_remote(manifest, remote_host=remote_host, remote_path=remote_path) if push else {"status": "skipped", "reason": "push_disabled"}
    return {"status": "ok" if pushed.get("status") in {"ok", "skipped"} else "blocked", "manifest": manifest, "local_write": local, "remote_sync": pushed, "calendar_write_attempted": False}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and optionally push sanitized coding signals for Rocky.")
    parser.add_argument("--output-path", dest="output_path")
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST, dest="remote_host")
    parser.add_argument("--remote-path", default=str(DEFAULT_REMOTE_PATH), dest="remote_path")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument("--no-push", action="store_false", dest="push", default=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_sync(output_path=args.output_path, remote_host=args.remote_host, remote_path=args.remote_path, push=args.push, limit=args.limit)
    if args.json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Coding signal sync: {payload.get('status')} ({(payload.get('remote_sync') or {}).get('status')})")
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
