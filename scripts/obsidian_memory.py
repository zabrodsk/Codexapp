from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_QMD_CONFIG_PATH = Path.home() / ".config" / "qmd" / "index.yml"
DEFAULT_QMD_DB_PATH = Path.home() / ".cache" / "qmd" / "index.sqlite"
DEFAULT_OBSIDIAN_VAULT_PATH = Path.home() / "Documents" / "VAULT" / "Rocky"
DEFAULT_OBSIDIAN_MEMORY_FOLDER = "OpenClaw Memory"
DEFAULT_COLLECTION_NAME = "obsidian-layer3"
DEFAULT_PROMOTION_LEDGER_PATH = Path(__file__).resolve().parents[1] / "improvement" / "obsidian_promotions.jsonl"
DEFAULT_CONTEXT_ROOT = "Obsidian Layer 3 durable notes from Rocky's vault."
DEFAULT_CONTEXT_MEMORY = "OpenClaw-managed durable notes written back safely by automation."
DEFAULT_PATTERN = "**/*.{md,markdown,txt,text}"
NOTE_TYPE_TO_FOLDER = {
    "project": "projects",
    "company": "companies",
    "person": "people",
    "theme": "themes",
    "weekly": "weekly",
    # Extended taxonomy for Dusan's work brain (WORK_BRAIN.md).
    # All additive — existing keys unchanged so prior promotions keep working.
    "deal": "deals",
    "meeting": "meetings",
    "decision": "decisions",
    "okr": "okrs",
    "weekly-report-cz": "weekly-reports",
    "area-moc": "areas",
    # Dusan's own curated notes for the weekly report, posted to Discord #focus.
    # Treated as highest-priority signal by Betty's weekly-boss-report SKILL.
    "inbox-note": "weekly-notes",
}
DEFAULT_IGNORE = [
    ".obsidian/**",
    ".trash/**",
    ".Trash/**",
    ".git/**",
    "node_modules/**",
    ".DS_Store",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.webp",
    "**/*.svg",
    "**/*.pdf",
    "**/*.zip",
]
SKILL_ENV_KEY = "obsidian-memory"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CONTROL_NOISE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class ObsidianMemoryConfig:
    enabled: bool
    vault_path: Path
    writeback_enabled: bool
    memory_folder: str
    openclaw_config_path: Path = DEFAULT_OPENCLAW_CONFIG_PATH
    qmd_config_path: Path = DEFAULT_QMD_CONFIG_PATH
    qmd_db_path: Path = DEFAULT_QMD_DB_PATH
    collection_name: str = DEFAULT_COLLECTION_NAME

    @property
    def memory_folder_path(self) -> Path:
        return self.vault_path / self.memory_folder

    @property
    def include_collection_by_default(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class DurableMemoryCandidate:
    agent_name: str
    note_type: str
    title: str
    summary: str
    body: str | None = None
    tags: list[str] | None = None
    source_ref: str | None = None
    source_date: str | None = None
    dedupe_key: str | None = None
    related_entities: list[str] | None = None


def load_obsidian_config() -> ObsidianMemoryConfig:
    openclaw_config_path = Path(
        os.getenv("OPENCLAW_CONFIG_PATH", str(DEFAULT_OPENCLAW_CONFIG_PATH))
    ).expanduser()
    openclaw_config = _read_openclaw_config(openclaw_config_path)
    skill_env = (
        openclaw_config.get("skills", {})
        .get("entries", {})
        .get(SKILL_ENV_KEY, {})
        .get("env", {})
    )

    def _value(key: str, default: str | None = None) -> str | None:
        value = os.getenv(key)
        if value is None:
            value = skill_env.get(key)
        if value is None:
            value = default
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _bool_value(key: str, default: bool) -> bool:
        value = _value(key)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    vault_path = Path(
        _value("OBSIDIAN_VAULT_PATH", str(DEFAULT_OBSIDIAN_VAULT_PATH))
        or str(DEFAULT_OBSIDIAN_VAULT_PATH)
    ).expanduser()
    memory_folder = (
        _value("OBSIDIAN_MEMORY_FOLDER", DEFAULT_OBSIDIAN_MEMORY_FOLDER)
        or DEFAULT_OBSIDIAN_MEMORY_FOLDER
    )

    return ObsidianMemoryConfig(
        enabled=_bool_value("OBSIDIAN_ENABLED", False),
        vault_path=vault_path,
        writeback_enabled=_bool_value("OBSIDIAN_WRITEBACK", False),
        memory_folder=memory_folder,
        openclaw_config_path=openclaw_config_path,
        qmd_config_path=Path(
            _value("OBSIDIAN_QMD_CONFIG_PATH", str(DEFAULT_QMD_CONFIG_PATH))
            or str(DEFAULT_QMD_CONFIG_PATH)
        ).expanduser(),
        qmd_db_path=Path(
            _value("OBSIDIAN_QMD_DB_PATH", str(DEFAULT_QMD_DB_PATH))
            or str(DEFAULT_QMD_DB_PATH)
        ).expanduser(),
        collection_name=(
            _value("OBSIDIAN_COLLECTION_NAME", DEFAULT_COLLECTION_NAME)
            or DEFAULT_COLLECTION_NAME
        ),
    )


def build_qmd_collection(config: ObsidianMemoryConfig) -> dict[str, Any]:
    context = {"/": DEFAULT_CONTEXT_ROOT}
    if config.memory_folder:
        context[f"/{config.memory_folder}"] = DEFAULT_CONTEXT_MEMORY
    return {
        "path": str(config.vault_path),
        "pattern": DEFAULT_PATTERN,
        "ignore": list(DEFAULT_IGNORE),
        "context": context,
        "includeByDefault": config.include_collection_by_default,
    }


def ensure_qmd_collection(config: ObsidianMemoryConfig) -> dict[str, Any]:
    qmd_config = _load_qmd_config(config.qmd_config_path)
    collections = qmd_config.setdefault("collections", {})
    collection = build_qmd_collection(config)
    changed = collections.get(config.collection_name) != collection
    collections[config.collection_name] = collection
    if changed:
        _write_qmd_config(config.qmd_config_path, qmd_config)
    return {
        "changed": changed,
        "config_path": str(config.qmd_config_path),
        "collection_name": config.collection_name,
        "collection": collection,
    }


def get_obsidian_status(config: ObsidianMemoryConfig | None = None) -> dict[str, Any]:
    config = config or load_obsidian_config()
    qmd_config = _load_qmd_config(config.qmd_config_path)
    collection = qmd_config.get("collections", {}).get(config.collection_name)
    backend = (
        _read_openclaw_config(config.openclaw_config_path)
        .get("memory", {})
        .get("backend")
    )

    status = "disabled"
    warning: str | None = None
    if config.enabled:
        if not config.vault_path.exists():
            status = "missing"
            warning = "vault path does not exist"
        elif not config.vault_path.is_dir():
            status = "invalid"
            warning = "vault path is not a directory"
        elif collection is None:
            status = "unconfigured"
            warning = "QMD collection is not configured"
        else:
            status = "ready"

    doc_count = 0
    embedded_count = 0
    active_collections: list[str] = []
    if config.qmd_db_path.exists():
        try:
            with sqlite3.connect(config.qmd_db_path) as conn:
                conn.row_factory = sqlite3.Row
                active_collections = [
                    str(row["name"])
                    for row in conn.execute(
                        "SELECT name FROM store_collections ORDER BY name"
                    ).fetchall()
                ]
                if collection is not None:
                    row = conn.execute(
                        """
                        SELECT
                            COUNT(*) AS doc_count,
                            COUNT(DISTINCT cv.hash) AS embedded_count
                        FROM documents d
                        LEFT JOIN content_vectors cv ON cv.hash = d.hash
                        WHERE d.collection = ? AND d.active = 1
                        """,
                        (config.collection_name,),
                    ).fetchone()
                    doc_count = int(row["doc_count"] or 0) if row else 0
                    embedded_count = int(row["embedded_count"] or 0) if row else 0
        except sqlite3.Error as exc:
            warning = f"QMD index inspection failed: {exc}"
    if warning is None and doc_count > embedded_count:
        warning = "Some indexed notes do not have embeddings yet; lexical retrieval is available, semantic retrieval may be limited."

    return {
        "status": status,
        "enabled": config.enabled,
        "vault_path": str(config.vault_path),
        "vault_exists": config.vault_path.exists(),
        "writeback_enabled": config.writeback_enabled,
        "memory_folder": config.memory_folder,
        "memory_folder_path": str(config.memory_folder_path),
        "collection_name": config.collection_name,
        "collection_configured": collection is not None,
        "qmd_config_path": str(config.qmd_config_path),
        "qmd_db_path": str(config.qmd_db_path),
        "indexed_notes": doc_count,
        "embedded_notes": embedded_count,
        "qmd_collections": active_collections,
        "openclaw_memory_backend": backend,
        "warning": warning,
    }


def sync_obsidian_vault(config: ObsidianMemoryConfig | None = None) -> dict[str, Any]:
    config = config or load_obsidian_config()
    ensure_result = ensure_qmd_collection(config)
    status = get_obsidian_status(config)
    if not config.enabled:
        return {
            "status": "skipped",
            "reason": "OBSIDIAN_ENABLED is false",
            "config": asdict(config),
            "ensure_result": ensure_result,
            "status_snapshot": status,
        }
    if not config.vault_path.exists():
        return {
            "status": "skipped",
            "reason": "vault path is missing",
            "config": asdict(config),
            "ensure_result": ensure_result,
            "status_snapshot": status,
        }

    update = _run_qmd_command(
        ["update"],
        db_path=config.qmd_db_path,
        config_path=config.qmd_config_path,
    )
    embed = _run_qmd_command(
        ["embed"],
        db_path=config.qmd_db_path,
        config_path=config.qmd_config_path,
    )
    snapshot = get_obsidian_status(config)
    sync_status = "ok"
    warning = None
    if update["returncode"] != 0:
        sync_status = "failed"
        warning = "qmd update failed"
    elif embed["returncode"] != 0:
        sync_status = "partial"
        warning = "qmd embed failed"
    return {
        "status": sync_status,
        "warning": warning,
        "ensure_result": ensure_result,
        "update": update,
        "embed": embed,
        "status_snapshot": snapshot,
    }


def query_obsidian_vault(
    query: str,
    *,
    limit: int = 5,
    mode: str = "query",
    config: ObsidianMemoryConfig | None = None,
) -> dict[str, Any]:
    config = config or load_obsidian_config()
    if not config.enabled:
        return {
            "status": "skipped",
            "reason": "OBSIDIAN_ENABLED is false",
            "query": query,
            "results": [],
        }
    if not config.vault_path.exists():
        return {
            "status": "skipped",
            "reason": "vault path is missing",
            "query": query,
            "results": [],
        }

    command = mode if mode in {"query", "search"} else "query"
    result = _run_qmd_command(
        [
            command,
            "--json",
            "--collection",
            config.collection_name,
            "-n",
            str(limit),
            query,
        ],
        db_path=config.qmd_db_path,
        config_path=config.qmd_config_path,
        timeout=45,
    )
    payload = None
    if result["returncode"] == 0:
        try:
            payload = _extract_json_payload(result["stdout"])
        except ValueError as exc:
            if command != "query":
                return {
                    "status": "failed",
                    "query": query,
                    "mode": command,
                    "error": str(exc),
                    "results": [],
                }
    elif command == "query":
        fallback = _run_qmd_command(
            [
                "search",
                "--json",
                "--collection",
                config.collection_name,
                "-n",
                str(limit),
                query,
            ],
            db_path=config.qmd_db_path,
            config_path=config.qmd_config_path,
            timeout=30,
        )
        if fallback["returncode"] == 0:
            try:
                payload = _extract_json_payload(fallback["stdout"])
                result = fallback
                command = "search"
            except ValueError as exc:
                return {
                    "status": "failed",
                    "query": query,
                    "mode": "search",
                    "error": str(exc),
                    "results": [],
                }
        else:
            return {
                "status": "failed",
                "query": query,
                "mode": command,
                "error": result["stderr"] or result["stdout"] or fallback["stderr"] or fallback["stdout"],
                "results": [],
            }
    else:
        return {
            "status": "failed",
            "query": query,
            "mode": command,
            "error": result["stderr"] or result["stdout"],
            "results": [],
        }

    hits = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
    formatted = [_normalize_qmd_hit(hit) for hit in hits[:limit]]
    return {
        "status": "ok",
        "query": query,
        "mode": command,
        "collection": config.collection_name,
        "results": formatted,
        "raw": payload,
    }


def write_obsidian_note(
    title: str,
    *,
    content: str,
    append: bool = False,
    config: ObsidianMemoryConfig | None = None,
) -> dict[str, Any]:
    config = config or load_obsidian_config()
    if not config.enabled:
        return {"status": "skipped", "reason": "OBSIDIAN_ENABLED is false"}
    if not config.writeback_enabled:
        return {"status": "skipped", "reason": "OBSIDIAN_WRITEBACK is false"}
    if not config.vault_path.exists():
        return {"status": "skipped", "reason": "vault path is missing"}

    filename = _slugify(title) or "openclaw-note"
    target_dir = config.memory_folder_path
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = (target_dir / f"{filename}.md").resolve()
    if target_dir.resolve() not in target_path.parents:
        raise ValueError("Refusing to write outside the dedicated Obsidian memory folder")

    if append and target_path.exists():
        existing = target_path.read_text(encoding="utf-8")
        next_text = existing.rstrip() + "\n\n" + content.rstrip() + "\n"
    else:
        next_text = content.rstrip() + "\n"
    target_path.write_text(next_text, encoding="utf-8")
    return {
        "status": "ok",
        "path": str(target_path),
        "append": append,
    }


def promote_cross_agent_memory(
    *,
    agent_name: str,
    note_type: str,
    title: str,
    summary: str,
    body: str | None = None,
    tags: list[str] | None = None,
    source_ref: str | None = None,
    source_date: str | None = None,
    dedupe_key: str | None = None,
    related_entities: list[str] | None = None,
    config: ObsidianMemoryConfig | None = None,
    ledger_path: Path = DEFAULT_PROMOTION_LEDGER_PATH,
) -> dict[str, Any]:
    config = config or load_obsidian_config()
    if not config.enabled:
        return {"status": "skipped", "reason": "OBSIDIAN_ENABLED is false"}
    if not config.vault_path.exists():
        return {"status": "skipped", "reason": "vault path is missing"}

    candidate = DurableMemoryCandidate(
        agent_name=_sanitize_agent_name(agent_name),
        note_type=_validate_note_type(note_type),
        title=_sanitize_memory_text("title", title, max_length=120),
        summary=_sanitize_memory_text("summary", summary, max_length=500),
        body=_sanitize_optional_body_text("body", body, max_length=200_000),
        tags=_sanitize_memory_list(tags or [], max_items=8, max_length=40),
        source_ref=_sanitize_optional_memory_text("source_ref", source_ref, max_length=200),
        source_date=_sanitize_optional_memory_text("source_date", source_date, max_length=40),
        dedupe_key=_sanitize_optional_memory_text("dedupe_key", dedupe_key, max_length=120),
        related_entities=_sanitize_memory_list(related_entities or [], max_items=8, max_length=80),
    )

    folder_name = NOTE_TYPE_TO_FOLDER[candidate.note_type]
    note_key = _slugify(candidate.dedupe_key or f"{candidate.note_type}-{candidate.title}") or "memory-note"
    target_dir = config.memory_folder_path / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = (target_dir / f"{note_key}.md").resolve()
    memory_root = config.memory_folder_path.resolve()
    if memory_root != target_path.parent and memory_root not in target_path.parents:
        raise ValueError("Refusing to write outside Rocky's guarded Obsidian memory area")

    existing_meta: dict[str, Any] = {}
    if target_path.exists():
        existing_meta = _read_frontmatter(target_path.read_text(encoding="utf-8"))

    source_agents = _merge_unique_strings(existing_meta.get("source_agents"), [candidate.agent_name], max_items=6)
    merged_tags = _merge_unique_strings(existing_meta.get("tags"), candidate.tags or [], max_items=12)
    source_refs = _merge_unique_strings(existing_meta.get("source_refs"), [candidate.source_ref] if candidate.source_ref else [], max_items=5)

    note_payload = {
        "type": candidate.note_type,
        "title": candidate.title,
        "source_agents": source_agents,
        "tags": merged_tags,
        "last_updated": _utc_now_iso(),
        "source_refs": source_refs,
        "related_entities": candidate.related_entities or [],
    }
    rendered = _render_promoted_note(
        metadata=note_payload,
        summary=candidate.summary,
        body=candidate.body,
        latest_agent=candidate.agent_name,
        source_ref=candidate.source_ref,
        source_date=candidate.source_date,
    )
    action = "updated" if target_path.exists() else "created"
    target_path.write_text(rendered, encoding="utf-8")
    _append_promotion_event(
        ledger_path,
        {
            "timestamp": _utc_now_iso(),
            "note_key": note_key,
            "note_path": str(target_path),
            "note_type": candidate.note_type,
            "agent_name": candidate.agent_name,
            "source_ref": candidate.source_ref,
            "action": action,
            "title": candidate.title,
        },
    )
    return {
        "status": "ok",
        "action": action,
        "path": str(target_path),
        "note_key": note_key,
        "note_type": candidate.note_type,
        "agent_name": candidate.agent_name,
    }


def read_recent_promotions(limit: int = 10, *, ledger_path: Path = DEFAULT_PROMOTION_LEDGER_PATH) -> list[dict[str, Any]]:
    if limit <= 0 or not ledger_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows[-limit:][::-1]


def format_recent_promotions(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No promotion events recorded yet."
    lines = [f"Recent Obsidian Promotions (latest {len(events)})", "=" * 60]
    for event in events:
        stamp = str(event.get("timestamp") or "?")[:19]
        action = event.get("action") or "unknown"
        agent_name = event.get("agent_name") or "unknown-agent"
        title = event.get("title") or "(untitled)"
        lines.append(f"[{action}] {agent_name} [{stamp}]")
        lines.append(f"  title: {title}")
        lines.append(f"  path: {event.get('note_path', '')}")
        if event.get("source_ref"):
            lines.append(f"  source: {event['source_ref']}")
    return "\n".join(lines)


def format_obsidian_status(payload: dict[str, Any]) -> str:
    lines = [
        f"Obsidian Layer 3 status: {payload['status']}",
        f"Enabled: {'yes' if payload['enabled'] else 'no'}",
        f"Vault: {payload['vault_path']}",
        f"Collection: {payload['collection_name']} ({'configured' if payload['collection_configured'] else 'missing'})",
        f"Indexed notes: {payload['indexed_notes']}",
        f"Embedded notes: {payload['embedded_notes']}",
        f"OpenClaw memory backend: {payload['openclaw_memory_backend'] or 'unknown'}",
    ]
    if payload.get("warning"):
        lines.append(f"Warning: {payload['warning']}")
    return "\n".join(lines)


def format_obsidian_query(payload: dict[str, Any]) -> str:
    if payload["status"] != "ok":
        reason = payload.get("reason") or payload.get("error") or "query failed"
        return f"Obsidian query failed for '{payload['query']}': {reason}"
    lines = [
        f"Obsidian results for '{payload['query']}' ({payload['collection']}):",
    ]
    results = payload.get("results") or []
    if not results:
        lines.append("No matching notes.")
        return "\n".join(lines)
    for item in results:
        title = item["title"] or item["path"] or "Untitled"
        score = item.get("score")
        score_text = f" score={score}" if score is not None else ""
        lines.append(f"- {title}{score_text}")
        if item.get("path"):
            lines.append(f"  path: {item['path']}")
        if item.get("snippet"):
            lines.append(f"  snippet: {item['snippet']}")
    return "\n".join(lines)


def _read_openclaw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_qmd_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"collections": {}}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {"collections": {}}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"collections": {}}
    except json.JSONDecodeError:
        return _load_qmd_config_via_node(path)


def _load_qmd_config_via_node(path: Path) -> dict[str, Any]:
    script = (
        "const fs=require('fs');"
        "const YAML=require('yaml');"
        "const data=YAML.parse(fs.readFileSync(process.argv[1],'utf8'))||{};"
        "process.stdout.write(JSON.stringify(data));"
    )
    result = subprocess.run(
        ["node", "-e", script, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Failed to parse QMD config: {result.stderr.strip() or result.stdout.strip()}")
    payload = json.loads(result.stdout)
    return payload if isinstance(payload, dict) else {"collections": {}}


def _write_qmd_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if "collections" not in payload or not isinstance(payload["collections"], dict):
        payload["collections"] = {}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_qmd_command(
    args: list[str],
    *,
    db_path: Path,
    config_path: Path,
    timeout: int = 120,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["XDG_CACHE_HOME"] = str(db_path.parent.parent)
    env["QMD_CONFIG_DIR"] = str(config_path.parent)
    env["INDEX_PATH"] = str(db_path)
    env["NO_COLOR"] = "1"
    command = ["qmd", *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": command,
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr + f"\nCommand timed out after {timeout}s.",
        }
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _extract_json_payload(text: str) -> Any:
    cleaned = _sanitize_qmd_stdout(text)
    if not cleaned:
        return []
    decoder = json.JSONDecoder()
    candidate_positions = [i for i, ch in enumerate(cleaned) if ch in "[{"]
    for index in candidate_positions:
        try:
            payload, _ = decoder.raw_decode(cleaned[index:])
            return payload
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Unable to parse QMD JSON output: {cleaned[:200]}")


def _sanitize_qmd_stdout(text: str) -> str:
    if not text:
        return ""
    cleaned = ANSI_ESCAPE_RE.sub("", text)
    cleaned = CONTROL_NOISE_RE.sub("", cleaned)
    # QMD progress UIs sometimes rewrite lines with carriage returns; keep only final visible text.
    cleaned = cleaned.replace("\r", "\n")
    return cleaned.strip()


def _normalize_qmd_hit(hit: dict[str, Any]) -> dict[str, Any]:
    path = str(
        hit.get("path")
        or hit.get("filepath")
        or hit.get("displayPath")
        or hit.get("file")
        or ""
    )
    if path.startswith("qmd://"):
        prefix = f"qmd://{DEFAULT_COLLECTION_NAME}/"
        if path.startswith(prefix):
            path = path[len(prefix):]
    snippet = str(hit.get("snippet") or hit.get("body") or "").strip()
    return {
        "title": str(hit.get("title") or "").strip(),
        "path": path,
        "docid": str(hit.get("docid") or "").strip() or None,
        "score": hit.get("score"),
        "snippet": re.sub(r"\s+", " ", snippet)[:280] if snippet else "",
        "context": str(hit.get("context") or "").strip() or None,
    }


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip().replace("/", "-")
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:80].strip("-")


def _sanitize_agent_name(value: str) -> str:
    cleaned = _sanitize_memory_text("agent_name", value, max_length=40).lower().replace(" ", "-")
    cleaned = re.sub(r"[^a-z0-9._-]+", "", cleaned)
    if not cleaned:
        raise ValueError("agent_name is required")
    return cleaned


def _validate_note_type(value: str) -> str:
    cleaned = _sanitize_memory_text("note_type", value, max_length=20).lower()
    if cleaned not in NOTE_TYPE_TO_FOLDER:
        allowed = ", ".join(sorted(NOTE_TYPE_TO_FOLDER))
        raise ValueError(f"note_type must be one of: {allowed}")
    return cleaned


def _sanitize_memory_text(field_name: str, value: str, *, max_length: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    cleaned = cleaned[:max_length].strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    _reject_secret_like(field_name, cleaned)
    return cleaned


def _sanitize_optional_memory_text(field_name: str, value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    cleaned = cleaned[:max_length].strip()
    if not cleaned:
        return None
    _reject_secret_like(field_name, cleaned)
    return cleaned


def _sanitize_optional_body_text(field_name: str, value: str | None, *, max_length: int) -> str | None:
    """Body sanitizer that preserves newlines (unlike the single-line sanitizer above).

    Used for durable-note bodies that may contain multi-paragraph content
    (e.g. email + attachment extractions). Collapses horizontal whitespace
    per line and caps blank-line runs at 2, but keeps paragraph breaks.
    """
    if value is None:
        return None
    raw = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in raw.split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip() + "\n…[truncated]"
    if not cleaned:
        return None
    _reject_secret_like(field_name, cleaned)
    return cleaned


def _sanitize_memory_list(values: list[str], *, max_items: int, max_length: int) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        item = _sanitize_optional_memory_text("list_item", value, max_length=max_length)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:max_items]


def _merge_unique_strings(existing: Any, incoming: list[str], *, max_items: int) -> list[str]:
    values: list[str] = []
    for collection in (existing or [], incoming):
        for item in collection:
            text = str(item).strip()
            if text and text not in values:
                values.append(text)
    return values[:max_items]


def _render_promoted_note(
    *,
    metadata: dict[str, Any],
    summary: str,
    body: str | None,
    latest_agent: str,
    source_ref: str | None,
    source_date: str | None,
) -> str:
    lines = [
        "---",
        f"type: {metadata['type']}",
        f"title: {metadata['title']}",
        f"last_updated: {metadata['last_updated']}",
        "source_agents:",
    ]
    for agent_name in metadata.get("source_agents") or []:
        lines.append(f"  - {agent_name}")
    if metadata.get("tags"):
        lines.append("tags:")
        for tag in metadata["tags"]:
            lines.append(f"  - {tag}")
    if metadata.get("source_refs"):
        lines.append("source_refs:")
        for ref in metadata["source_refs"]:
            lines.append(f"  - {ref}")
    if metadata.get("related_entities"):
        lines.append("related_entities:")
        for item in metadata["related_entities"]:
            lines.append(f"  - {item}")
    lines.extend(
        [
            "---",
            "",
            "## Summary",
            summary,
        ]
    )
    if body:
        lines.extend(["", "## Durable Context", body])
    provenance_bits = [f"latest agent: {latest_agent}"]
    if source_date:
        provenance_bits.append(f"date: {source_date}")
    if source_ref:
        provenance_bits.append(f"source: {source_ref}")
    lines.extend(["", "## Provenance", " | ".join(provenance_bits)])
    return "\n".join(lines).rstrip() + "\n"


def _append_promotion_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _read_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}
    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("  - ") and current_list_key:
            data.setdefault(current_list_key, []).append(line[4:].strip())
            continue
        if ":" not in line:
            current_list_key = None
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value:
            data[key] = value
            current_list_key = None
        else:
            data[key] = []
            current_list_key = key
    return data


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reject_secret_like(field_name: str, value: str) -> None:
    secret_patterns = [
        re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
        re.compile(r"\bntn_[A-Za-z0-9]{10,}\b"),
        re.compile(r"\bapify_api_[A-Za-z0-9]{10,}\b"),
        re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    ]
    for pattern in secret_patterns:
        if pattern.search(value):
            raise ValueError(f"{field_name} looks like it contains a secret; summarize it instead")
