#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LAYER3_ROOT = ROOT / "memory" / "layer3"
INDEX_DIR = LAYER3_ROOT / "indexes"
VALIDATION_REPORT_PATH = INDEX_DIR / "validation-report.json"
ENTITY_INDEX_PATH = INDEX_DIR / "entity-index.json"
BACKLINKS_PATH = INDEX_DIR / "backlinks.json"

REQUIRED_FIELDS = {
    "title",
    "note_type",
    "schema_version",
    "last_updated",
    "status",
    "confidence",
    "owner",
}
ALLOWED_NOTE_TYPES = {
    "Project State",
    "Person Brief",
    "Company Brief",
    "Deal Brief",
    "Meeting Brief",
    "Decision Log",
    "Weekly Synthesis",
    "Open Loop",
}
ALLOWED_STATUSES = {"active", "stale", "archived"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
REQUIRED_SECTIONS = {
    "Current state",
    "Timeline",
    "Typed links",
    "Open loops",
    "Evidence / provenance",
}
OPTIONAL_SECTIONS = {
    "Operating rules",
    "Known entities",
    "Known races",
    "Decisions",
    "Risks",
    "Retired / historical context",
    "What this note is for",
    "Monitoring setup",
    "Known reliability history",
    "Operating rules for future updates",
}
RELATION_TYPES = {
    "person",
    "company",
    "project",
    "deal",
    "meeting",
    "decision",
    "channel",
    "email-thread",
    "source",
    "agent",
    "system",
}
TYPE_TO_FOLDER = {
    "project": "projects",
    "person": "people",
    "company": "companies",
    "deal": "deals",
    "meeting": "meetings",
    "decision": "decisions",
    "weekly": "weekly",
    "open-loop": "open-loops",
}
TYPE_TO_NOTE_TYPE = {
    "project": "Project State",
    "person": "Person Brief",
    "company": "Company Brief",
    "deal": "Deal Brief",
    "meeting": "Meeting Brief",
    "decision": "Decision Log",
    "weekly": "Weekly Synthesis",
    "open-loop": "Open Loop",
}
FRONTMATTER_ORDER = [
    "title",
    "note_type",
    "schema_version",
    "last_updated",
    "status",
    "confidence",
    "owner",
    "related_people",
    "related_projects",
    "related_companies",
    "related_channels",
    "source_daily",
    "source_refs",
    "tags",
]
SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret_key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}",
        r"\bsk-[A-Za-z0-9]{20,}",
        r"\bghp_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    )
]
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
TYPED_LINK_RE = re.compile(r"^\s*-\s*([a-z][a-z0-9-]*):\s*(.+?)\s*$")


@dataclass(frozen=True)
class Layer3Note:
    path: Path
    relative_path: str
    frontmatter: dict[str, Any]
    body: str
    raw: str


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def normalize_entity(relation_type: str, value: str) -> str:
    compact = re.sub(r"\s+", " ", value.strip())
    return f"{relation_type.strip().lower()}:{compact}"


def parse_frontmatter(text: str, path: Path | None = None) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        label = str(path) if path else "note"
        raise ValueError(f"{label}: YAML frontmatter is missing")
    end = text.find("\n---", 4)
    if end == -1:
        label = str(path) if path else "note"
        raise ValueError(f"{label}: YAML frontmatter is not closed")
    frontmatter_text = text[4:end].strip("\n")
    body_start = end + len("\n---")
    if text[body_start : body_start + 1] == "\n":
        body_start += 1
    return _parse_simple_yaml(frontmatter_text), text[body_start:]


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        list_match = re.match(r"^\s*-\s*(.*)$", raw_line)
        if list_match and current_key:
            result.setdefault(current_key, []).append(_parse_scalar(list_match.group(1)))
            continue
        key_match = re.match(r"^([A-Za-z0-9_-]+):(?:\s*(.*))?$", raw_line)
        if key_match is None:
            continue
        key, raw_value = key_match.group(1), key_match.group(2) or ""
        current_key = key
        if raw_value == "":
            result[key] = []
        elif raw_value == "[]":
            result[key] = []
        elif raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            result[key] = [] if not inner else [_parse_scalar(item.strip()) for item in inner.split(",")]
        else:
            result[key] = _parse_scalar(raw_value)
    return result


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.isdigit():
        return int(value)
    return value


def dump_frontmatter(frontmatter: dict[str, Any]) -> str:
    lines = ["---"]
    keys = [key for key in FRONTMATTER_ORDER if key in frontmatter]
    keys.extend(sorted(key for key in frontmatter if key not in FRONTMATTER_ORDER))
    for key in keys:
        value = frontmatter[key]
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_format_scalar(item)}")
            else:
                lines.append(f"{key}: []")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _format_scalar(value: Any) -> str:
    text = str(value)
    if not text or text.strip() != text or text.startswith(("{", "[", "&", "*", "#")):
        return json.dumps(text)
    if ": " in text:
        return json.dumps(text)
    return text


def load_note(path: Path) -> Layer3Note:
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw, path)
    return Layer3Note(
        path=path,
        relative_path=_rel(path),
        frontmatter=frontmatter,
        body=body,
        raw=raw,
    )


def iter_note_paths(root: Path = LAYER3_ROOT) -> list[Path]:
    if not root.exists():
        return []
    paths = []
    for path in root.rglob("*.md"):
        if INDEX_DIR in path.parents:
            continue
        paths.append(path)
    return sorted(paths)


def extract_sections(body: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip()] = body[start:end].strip()
    return sections


def parse_typed_links(body: str) -> list[tuple[str, str]]:
    sections = extract_sections(body)
    content = sections.get("Typed links", "")
    links: list[tuple[str, str]] = []
    for line in content.splitlines():
        match = TYPED_LINK_RE.match(line)
        if match:
            links.append((match.group(1).lower(), match.group(2).strip()))
    return links


def _frontmatter_typed_links(frontmatter: dict[str, Any]) -> list[tuple[str, str]]:
    mapping = {
        "related_people": "person",
        "related_projects": "project",
        "related_companies": "company",
        "related_channels": "channel",
        "source_daily": "source",
        "source_refs": "source",
    }
    links: list[tuple[str, str]] = []
    for key, relation_type in mapping.items():
        values = frontmatter.get(key, [])
        if isinstance(values, str):
            values = [values]
        for value in values:
            links.append((relation_type, str(value)))
    return links


def all_typed_links(note: Layer3Note) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for relation_type, value in [*parse_typed_links(note.body), *_frontmatter_typed_links(note.frontmatter)]:
        entity = normalize_entity(relation_type, value)
        if entity not in seen:
            seen.add(entity)
            result.append((relation_type, value))
    return result


def validate_note(note: Layer3Note, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    frontmatter = note.frontmatter
    missing = sorted(REQUIRED_FIELDS - set(frontmatter))
    for field in missing:
        errors.append(f"missing required field: {field}")
    if frontmatter.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if frontmatter.get("note_type") not in ALLOWED_NOTE_TYPES:
        errors.append(f"note_type must be one of: {', '.join(sorted(ALLOWED_NOTE_TYPES))}")
    if frontmatter.get("status") not in ALLOWED_STATUSES:
        errors.append("status must be active, stale, or archived")
    if frontmatter.get("confidence") not in ALLOWED_CONFIDENCE:
        errors.append("confidence must be high, medium, or low")

    sections = set(extract_sections(note.body))
    for section in sorted(REQUIRED_SECTIONS - sections):
        errors.append(f"missing required section: {section}")

    for relation_type, value in parse_typed_links(note.body):
        if relation_type not in RELATION_TYPES:
            errors.append(f"invalid typed link relation: {relation_type}")
        if not value:
            errors.append(f"empty typed link value for relation: {relation_type}")

    source_daily = frontmatter.get("source_daily", [])
    if isinstance(source_daily, str):
        source_daily = [source_daily]
    for source_path in source_daily:
        source_text = str(source_path)
        if source_text and not re.match(r"^[a-z]+:", source_text) and not (root / source_text).exists():
            errors.append(f"source_daily path does not exist: {source_text}")

    secret_hits = _secret_hits(note.raw)
    for hit in secret_hits:
        errors.append(f"secret-like text detected: {hit}")
    return errors


def _secret_hits(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(match.group(0)[:80])
    return hits


def validate_all(root: Path = LAYER3_ROOT, write_report: bool = True) -> tuple[int, dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    total_errors = 0
    runtime_errors: list[str] = []
    for path in iter_note_paths(root):
        try:
            note = load_note(path)
            errors = validate_note(note)
        except Exception as exc:  # noqa: BLE001 - CLI should report every note, not crash first.
            errors = [str(exc)]
        total_errors += len(errors)
        notes.append({"path": _rel(path), "valid": not errors, "errors": errors})
    report = {
        "valid": total_errors == 0 and not runtime_errors,
        "note_count": len(notes),
        "error_count": total_errors,
        "notes": notes,
        "runtime_errors": runtime_errors,
    }
    if write_report:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        VALIDATION_REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return (0 if report["valid"] else 1), report


def build_indexes(root: Path = LAYER3_ROOT) -> tuple[dict[str, Any], dict[str, Any]]:
    entity_index: dict[str, Any] = {"entities": {}, "notes": {}}
    backlinks: dict[str, list[str]] = {}
    for path in iter_note_paths(root):
        note = load_note(path)
        errors = validate_note(note)
        if errors:
            raise ValueError(f"{note.relative_path} is invalid; run validate first")
        links = [normalize_entity(relation_type, value) for relation_type, value in all_typed_links(note)]
        entity_index["notes"][note.relative_path] = {
            "title": str(note.frontmatter.get("title", "")),
            "note_type": str(note.frontmatter.get("note_type", "")),
            "status": str(note.frontmatter.get("status", "")),
            "confidence": str(note.frontmatter.get("confidence", "")),
            "links": sorted(links),
        }
        for entity in links:
            relation_type = entity.split(":", 1)[0]
            item = entity_index["entities"].setdefault(entity, {"mentions": [], "types": [relation_type]})
            item["mentions"].append(note.relative_path)
            backlinks.setdefault(entity, []).append(note.relative_path)
    for entity in entity_index["entities"].values():
        entity["mentions"] = sorted(set(entity["mentions"]))
        entity["types"] = sorted(set(entity["types"]))
    backlinks = {key: sorted(set(value)) for key, value in sorted(backlinks.items())}
    entity_index["entities"] = dict(sorted(entity_index["entities"].items()))
    entity_index["notes"] = dict(sorted(entity_index["notes"].items()))
    return entity_index, backlinks


def write_indexes() -> tuple[dict[str, Any], dict[str, Any]]:
    entity_index, backlinks = build_indexes()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    ENTITY_INDEX_PATH.write_text(json.dumps(entity_index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    BACKLINKS_PATH.write_text(json.dumps(backlinks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entity_index, backlinks


def query_notes(query: str, root: Path = LAYER3_ROOT) -> list[dict[str, Any]]:
    query_norm = query.strip().lower()
    entity_query = query_norm if ":" in query_norm else None
    results: list[tuple[int, str, dict[str, Any]]] = []
    for path in iter_note_paths(root):
        note = load_note(path)
        title = str(note.frontmatter.get("title", ""))
        filename = path.stem.replace("-", " ")
        entities = [normalize_entity(relation_type, value) for relation_type, value in all_typed_links(note)]
        score = 0
        if title.lower() == query_norm:
            score = 100
        elif entity_query and entity_query in [entity.lower() for entity in entities]:
            score = 90
        elif any(entity.lower().endswith(f":{query_norm}") for entity in entities):
            score = 85
        elif query_norm in title.lower():
            score = 70
        elif query_norm in filename.lower():
            score = 60
        elif query_norm in note.body.lower():
            score = 40
        if score:
            sections = extract_sections(note.body)
            results.append(
                (
                    -score,
                    note.relative_path,
                    {
                        "path": note.relative_path,
                        "title": title,
                        "score": score,
                        "current_state": _excerpt(sections.get("Current state", "")),
                        "open_loops": _lines(sections.get("Open loops", "")),
                        "typed_links": entities,
                        "source_refs": _source_refs(note),
                    },
                )
            )
    return [payload for _, _, payload in sorted(results)]


def find_backlinks(entity: str, root: Path = LAYER3_ROOT) -> dict[str, Any]:
    entity_norm = entity.strip().lower()
    matches: dict[str, list[str]] = {}
    for path in iter_note_paths(root):
        note = load_note(path)
        for relation_type, value in all_typed_links(note):
            normalized = normalize_entity(relation_type, value)
            if normalized.lower() == entity_norm or value.strip().lower() == entity_norm:
                matches.setdefault(normalized, []).append(note.relative_path)
    return {"query": entity, "matches": {key: sorted(set(value)) for key, value in sorted(matches.items())}}


def _excerpt(value: str, limit: int = 900) -> str:
    value = re.sub(r"\n{3,}", "\n\n", value.strip())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _source_refs(note: Layer3Note) -> list[str]:
    refs: list[str] = []
    for key in ("source_daily", "source_refs"):
        values = note.frontmatter.get(key, [])
        if isinstance(values, str):
            values = [values]
        refs.extend(str(value) for value in values)
    sections = extract_sections(note.body)
    refs.extend(line.strip().removeprefix("- ").removeprefix("Source: ").strip() for line in sections.get("Evidence / provenance", "").splitlines() if line.strip())
    return sorted(set(ref for ref in refs if ref))


def migrate_text(path: Path) -> str:
    note = load_note(path)
    frontmatter = dict(note.frontmatter)
    today = date.today().isoformat()
    frontmatter.setdefault("schema_version", 1)
    frontmatter.setdefault("last_updated", today)
    frontmatter.setdefault("status", "active")
    frontmatter.setdefault("confidence", "medium")
    frontmatter.setdefault("owner", "Rocky")
    frontmatter.setdefault("related_people", [])
    frontmatter.setdefault("related_projects", [str(frontmatter.get("title", path.stem.replace("-", " ").title()))])
    frontmatter.setdefault("related_companies", [])
    frontmatter.setdefault("related_channels", [])
    frontmatter.setdefault("source_daily", [])
    frontmatter.setdefault("source_refs", [])
    tags = frontmatter.setdefault("tags", [])
    if isinstance(tags, str):
        tags = [tags]
        frontmatter["tags"] = tags
    if "memory/layer3" not in tags:
        tags.append("memory/layer3")

    body = note.body.lstrip("\n").rstrip() + "\n"
    sections = extract_sections(body)
    title = str(frontmatter.get("title", path.stem.replace("-", " ").title()))
    insertion: list[str] = []
    if "Current state" not in sections:
        insertion.append(
            "## Current state\n"
            "This note is the current durable Layer 3 state. Existing sections below remain authoritative until they are intentionally synthesized further.\n"
        )
    if "Timeline" not in sections:
        source = _first_source(frontmatter) or note.relative_path
        insertion.append(f"## Timeline\n- {frontmatter['last_updated']} - Layer 3 note captured or refreshed. Source: {source}\n")
    if "Typed links" not in sections:
        links = _frontmatter_typed_links(frontmatter)
        link_lines = [f"- {relation_type}: {value}" for relation_type, value in links]
        if not link_lines:
            link_lines = [f"- project: {title}"]
        insertion.append("## Typed links\n" + "\n".join(link_lines) + "\n")
    if "Evidence / provenance" not in sections:
        sources = [*_as_list(frontmatter.get("source_daily", [])), *_as_list(frontmatter.get("source_refs", []))]
        if not sources:
            sources = [note.relative_path]
        insertion.append("## Evidence / provenance\n" + "\n".join(f"- Source: {source}" for source in sources) + "\n")

    if insertion:
        heading = f"# {title}"
        lines = body.splitlines()
        if lines and lines[0].strip() == heading:
            body = "\n".join([lines[0], "", *insertion, *lines[1:]]).rstrip() + "\n"
        else:
            body = "\n".join([f"# {title}", "", *insertion, body]).rstrip() + "\n"

    return dump_frontmatter(frontmatter) + "\n" + body


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _first_source(frontmatter: dict[str, Any]) -> str | None:
    for key in ("source_daily", "source_refs"):
        values = _as_list(frontmatter.get(key, []))
        if values:
            return values[0]
    return None


def new_note_text(note_type_key: str, title: str) -> tuple[Path, str]:
    if note_type_key not in TYPE_TO_NOTE_TYPE:
        raise ValueError(f"unknown type {note_type_key}; expected one of {', '.join(sorted(TYPE_TO_NOTE_TYPE))}")
    folder = TYPE_TO_FOLDER[note_type_key]
    path = LAYER3_ROOT / folder / f"{_slugify(title)}.md"
    frontmatter = {
        "title": title,
        "note_type": TYPE_TO_NOTE_TYPE[note_type_key],
        "schema_version": 1,
        "last_updated": date.today().isoformat(),
        "status": "active",
        "confidence": "medium",
        "owner": "Rocky",
        "related_people": [],
        "related_projects": [title] if note_type_key == "project" else [],
        "related_companies": [],
        "related_channels": [],
        "source_daily": [],
        "source_refs": [],
        "tags": ["memory/layer3"],
    }
    body = (
        f"# {title}\n\n"
        "## Current state\n\n"
        "## Timeline\n\n"
        "## Typed links\n\n"
        "## Open loops\n\n"
        "## Evidence / provenance\n"
    )
    return path, dump_frontmatter(frontmatter) + "\n" + body


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _cmd_validate(_: argparse.Namespace) -> int:
    exit_code, report = validate_all()
    valid_count = sum(1 for note in report["notes"] if note["valid"])
    print(f"Layer 3 validation: {valid_count}/{report['note_count']} valid, {report['error_count']} error(s)")
    for note in report["notes"]:
        if note["errors"]:
            print(f"- {note['path']}")
            for error in note["errors"]:
                print(f"  - {error}")
    print(f"Report: {_rel(VALIDATION_REPORT_PATH)}")
    return exit_code


def _cmd_index(_: argparse.Namespace) -> int:
    try:
        entity_index, backlinks = write_indexes()
    except Exception as exc:  # noqa: BLE001
        print(f"index failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"Indexed {len(entity_index['notes'])} note(s), {len(entity_index['entities'])} entit(ies). "
        f"Wrote {_rel(ENTITY_INDEX_PATH)} and {_rel(BACKLINKS_PATH)}"
    )
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    _print_json({"query": args.query, "results": query_notes(args.query)})
    return 0


def _cmd_backlinks(args: argparse.Namespace) -> int:
    _print_json(find_backlinks(args.entity))
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    path = (ROOT / args.path).resolve() if not Path(args.path).is_absolute() else Path(args.path)
    try:
        new_text = migrate_text(path)
    except Exception as exc:  # noqa: BLE001
        print(f"migrate failed: {exc}", file=sys.stderr)
        return 2
    old_text = path.read_text(encoding="utf-8")
    if args.dry_run:
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (migrated)",
        )
        print("".join(diff), end="")
        return 0
    path.write_text(new_text, encoding="utf-8")
    errors = validate_note(load_note(path))
    if errors:
        print("migrated note is invalid:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Migrated {_rel(path)}")
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    try:
        path, text = new_note_text(args.type, args.title)
    except Exception as exc:  # noqa: BLE001
        print(f"new failed: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"Path: {_rel(path)}")
        print(text, end="")
        return 0
    if path.exists():
        print(f"new failed: {path} already exists", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Created {_rel(path)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic Layer 3 markdown memory helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate").set_defaults(func=_cmd_validate)
    subparsers.add_parser("index").set_defaults(func=_cmd_index)
    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("query")
    query_parser.set_defaults(func=_cmd_query)
    backlinks_parser = subparsers.add_parser("backlinks")
    backlinks_parser.add_argument("entity")
    backlinks_parser.set_defaults(func=_cmd_backlinks)
    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--path", required=True)
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.set_defaults(func=_cmd_migrate)
    new_parser = subparsers.add_parser("new")
    new_parser.add_argument("--type", required=True, choices=sorted(TYPE_TO_NOTE_TYPE))
    new_parser.add_argument("--title", required=True)
    new_parser.add_argument("--dry-run", action="store_true")
    new_parser.set_defaults(func=_cmd_new)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
