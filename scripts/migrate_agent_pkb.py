#!/usr/bin/env python3
"""Migrate the Agent PKB pilot from numbered folders to document layers.

The Markdown manifest is the source of truth.  The command is a dry run unless
``--apply`` is passed.  Apply mode makes a byte-for-byte backup below the
hidden ``.migration-backups`` directory before moving or rewriting anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)", re.DOTALL)
VALID_STATUS = {"canonical", "draft", "active", "superseded"}
VALID_AUTHORITY = {"curated", "researched", "imported", "generated"}
SKIPPED_LINK_DIRS = {
    "_archive",
    "_concepts",
    "_materials",
    "_origin",
    "_review",
    "_trash",
}


@dataclass(frozen=True)
class MarkdownEntry:
    source: Path
    target: Path
    doc_type: str
    canonical_id: str


@dataclass(frozen=True)
class BinaryEntry:
    source: Path
    target: Path


@dataclass(frozen=True)
class PlannedMarkdown:
    source: Path
    target: Path
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.source != self.target or self.before != self.after


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_manifest(path: Path) -> tuple[list[MarkdownEntry], list[BinaryEntry]]:
    markdown: list[MarkdownEntry] = []
    binaries: list[BinaryEntry] = []
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "## Markdown (77)":
            section = "markdown"
            continue
        if line == "## 원본 파일":
            section = "binary"
            continue
        if line.startswith("## "):
            section = ""
            continue
        if not line.startswith("| agent/"):
            continue
        cells = _table_cells(line)
        if section == "markdown" and len(cells) == 5:
            markdown.append(
                MarkdownEntry(Path(cells[0]), Path(cells[1]), cells[2], cells[3])
            )
        elif section == "binary" and len(cells) == 3:
            binaries.append(BinaryEntry(Path(cells[0]), Path(cells[1])))
    if not markdown or not binaries:
        raise ValueError(f"manifest tables could not be parsed: {path}")
    return markdown, binaries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_document(text: str, path: Path) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        if text.startswith("---\n") or text.startswith("---\r\n"):
            raise ValueError(f"malformed frontmatter: {path}")
        return {}, text
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"frontmatter must be a mapping: {path}")
    return metadata, match.group(2)


def _title(metadata: dict[str, Any], body: str, path: Path) -> str:
    value = metadata.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    heading = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if heading:
        return heading.group(1).strip().strip("#").strip()
    return path.stem


def _normalise_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip().lstrip("#") for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip().lstrip("#") for item in value]
    else:
        items = []
    return [item for item in items if item]


def _inferred_tags(entry: MarkdownEntry) -> list[str]:
    parts = entry.target.parts
    tags = ["agent", entry.doc_type]
    for layer in ("concepts", "guides", "research"):
        if layer in parts:
            index = parts.index(layer)
            if index + 1 < len(parts) - 1:
                tags.append(parts[index + 1])
            break
    return tags


def _ordered_metadata(
    original: dict[str, Any], entry: MarkdownEntry, body: str
) -> dict[str, Any]:
    status_default = "canonical" if entry.doc_type == "concept" else "active"
    authority_default = "researched" if entry.doc_type == "research" else "curated"
    current_status = original.get("status")
    current_authority = original.get("authority")
    tags = _normalise_tags(original.get("tags"))
    for tag in _inferred_tags(entry):
        if tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)

    required: dict[str, Any] = {
        "schema_version": 1,
        "title": _title(original, body, entry.target),
        "doc_type": entry.doc_type,
        "canonical_id": entry.canonical_id,
        "status": current_status if current_status in VALID_STATUS else status_default,
        "authority": (
            current_authority
            if current_authority in VALID_AUTHORITY
            else authority_default
        ),
        "tags": tags,
    }
    return required | {
        key: value for key, value in original.items() if key not in required
    }


def normalise_document(text: str, entry: MarkdownEntry) -> str:
    metadata, body = _parse_document(text, entry.source)
    block = yaml.safe_dump(
        _ordered_metadata(metadata, entry, body),
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )
    return f"---\n{block}---\n\n{body.lstrip()}"


def _path_replacements(
    markdown: list[MarkdownEntry], binaries: list[BinaryEntry]
) -> list[tuple[str, str]]:
    replacements: set[tuple[str, str]] = set()
    for entry in [*markdown, *binaries]:
        if entry.source == entry.target:
            continue
        old = entry.source.as_posix()
        new = entry.target.as_posix()
        replacements.add((old, new))
        replacements.add((f"data/{old}", f"data/{new}"))
        if old.endswith(".md"):
            replacements.add((old[:-3], new[:-3]))
        replacements.add((f"PKB/{old}", f"PKB/{new}"))
        if old.endswith(".md"):
            replacements.add((f"PKB/{old[:-3]}", f"PKB/{new[:-3]}"))
    return sorted(replacements, key=lambda pair: len(pair[0]), reverse=True)


def rewrite_paths(text: str, replacements: list[tuple[str, str]]) -> str:
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _current_path(root: Path, source: Path, target: Path) -> Path:
    source_path = root / source
    target_path = root / target
    if source == target:
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        return source_path
    if source_path.exists() and target_path.exists():
        raise FileExistsError(f"source and target both exist: {source_path} -> {target_path}")
    if source_path.is_file():
        return source_path
    if target_path.is_file():
        return target_path
    raise FileNotFoundError(f"source and target are both missing: {source_path} -> {target_path}")


def build_plan(
    root: Path,
    markdown: list[MarkdownEntry],
    binaries: list[BinaryEntry],
) -> tuple[list[PlannedMarkdown], list[tuple[Path, Path]]]:
    targets = [entry.target for entry in markdown]
    canonical_ids = [entry.canonical_id for entry in markdown]
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate Markdown target in manifest")
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("duplicate canonical_id in manifest")

    replacements = _path_replacements(markdown, binaries)
    planned: list[PlannedMarkdown] = []
    entry_sources: set[Path] = set()
    for entry in markdown:
        current = _current_path(root, entry.source, entry.target)
        entry_sources.add(current.resolve())
        before = current.read_text(encoding="utf-8")
        after = rewrite_paths(normalise_document(before, entry), replacements)
        planned.append(PlannedMarkdown(current, root / entry.target, before, after))

    # Update explicit path references in notes outside the manifest as well.
    for path in root.rglob("*.md"):
        relative_parts = path.relative_to(root).parts
        if any(part.startswith(".") or part in SKIPPED_LINK_DIRS for part in relative_parts):
            continue
        if path.resolve() in entry_sources:
            continue
        before = path.read_text(encoding="utf-8")
        after = rewrite_paths(before, replacements)
        if before != after:
            planned.append(PlannedMarkdown(path, path, before, after))

    # Evaluation fixtures carry doc_id values rather than Markdown links.
    # Keep them aligned with physical moves even though hidden directories are
    # deliberately excluded from the normal note rewrite above.
    eval_root = root / ".eval"
    if eval_root.is_dir():
        for path in eval_root.rglob("*.jsonl"):
            before = path.read_text(encoding="utf-8")
            after = rewrite_paths(before, replacements)
            if before != after:
                planned.append(PlannedMarkdown(path, path, before, after))

    binary_moves: list[tuple[Path, Path]] = []
    for entry in binaries:
        current = _current_path(root, entry.source, entry.target)
        target = root / entry.target
        if current != target:
            binary_moves.append((current, target))
    return planned, binary_moves


def _backup(
    root: Path,
    backup_root: Path,
    planned: list[PlannedMarkdown],
    binary_moves: list[tuple[Path, Path]],
) -> None:
    records: list[dict[str, str]] = []
    sources = [item.source for item in planned if item.changed]
    sources.extend(source for source, _ in binary_moves)
    targets = {
        item.source: item.target for item in planned if item.changed
    } | dict(binary_moves)
    for source in sorted(set(sources)):
        relative = source.relative_to(root)
        destination = backup_root / "original" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        records.append(
            {
                "source": relative.as_posix(),
                "target": targets[source].relative_to(root).as_posix(),
                "sha256": _sha256(source),
            }
        )
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "manifest.json").write_text(
        json.dumps({"created_at": datetime.now(UTC).isoformat(), "files": records}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def apply_plan(
    root: Path,
    backup_root: Path,
    planned: list[PlannedMarkdown],
    binary_moves: list[tuple[Path, Path]],
) -> None:
    _backup(root, backup_root, planned, binary_moves)
    for item in planned:
        if not item.changed:
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        if item.source != item.target:
            item.source.replace(item.target)
        item.target.write_text(item.after, encoding="utf-8")
    for source, target in binary_moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    # Remove only directories made empty by the moves; backups remain hidden.
    for path in sorted((root / "agent").rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def migrate_graph(
    graph_db: Path,
    backup_root: Path,
    markdown: list[MarkdownEntry],
) -> dict[str, int]:
    """Back up SQLite and rename provenance IDs for moved Markdown notes."""
    if not graph_db.is_file():
        raise FileNotFoundError(f"graph database not found: {graph_db}")
    backup_root.mkdir(parents=True, exist_ok=True)
    graph_backup = backup_root / "pkb_graph.sqlite"
    with sqlite3.connect(str(graph_db)) as source, sqlite3.connect(str(graph_backup)) as target:
        source.backup(target)

    from pkb.graph.schema import graph_connection
    from pkb.graph.store import rename_document

    totals = {
        "documents": 0,
        "concept_mentions": 0,
        "concept_edge_evidence": 0,
        "extracted_chunks": 0,
    }
    with graph_connection(str(graph_db)) as conn:
        for entry in markdown:
            if entry.source == entry.target:
                continue
            result = rename_document(
                conn,
                f"data/{entry.source.as_posix()}",
                f"data/{entry.target.as_posix()}",
            )
            for key, value in result.items():
                totals[key] += value
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="PKB root directory")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs/pkb-agent-migration-manifest.md",
    )
    parser.add_argument("--apply", action="store_true", help="apply after preflight and backup")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument(
        "--graph-db",
        type=Path,
        default=None,
        help="optional SQLite graph whose document provenance IDs should move too",
    )
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"PKB root does not exist: {root}")
    markdown, binaries = parse_manifest(args.manifest.resolve())
    planned, binary_moves = build_plan(root, markdown, binaries)
    changed_markdown = [item for item in planned if item.changed]
    moved_markdown = [item for item in changed_markdown if item.source != item.target]
    print(
        f"preflight ok: markdown={len(markdown)}, changed={len(changed_markdown)}, "
        f"moved={len(moved_markdown)}, binaries={len(binary_moves)}"
    )
    for item in moved_markdown:
        print(f"  MOVE {item.source.relative_to(root)} -> {item.target.relative_to(root)}")
    if not args.apply:
        print("dry-run only; pass --apply to write")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_root = (
        args.backup_dir.expanduser().resolve()
        if args.backup_dir
        else root / ".migration-backups" / f"agent-{stamp}"
    )
    if backup_root.exists():
        raise FileExistsError(f"backup directory already exists: {backup_root}")
    apply_plan(root, backup_root, planned, binary_moves)
    if args.graph_db is not None:
        graph_stats = migrate_graph(args.graph_db.expanduser().resolve(), backup_root, markdown)
        print(f"graph migrated: {json.dumps(graph_stats, ensure_ascii=False)}")
    print(f"applied; backup={backup_root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
