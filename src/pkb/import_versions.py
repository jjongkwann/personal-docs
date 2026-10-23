"""Explicit immutable import identities and data-preserving legacy migration."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from pkb import access
from pkb.config import data_dir
from pkb.ingest import ingest_files, parse_frontmatter


def validate_identity(document_id: object, version: object) -> None:
    if not isinstance(document_id, str):
        raise ValueError("document_id는 소문자 UUID여야 합니다.")
    try:
        valid = str(UUID(document_id)) == document_id
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("document_id는 소문자 UUID여야 합니다.")
    if type(version) is not int or version < 1:
        raise ValueError("version은 양의 정수여야 합니다.")


@contextmanager
def import_lock():
    """Serialize import slot validation/creation and migration across processes."""
    from pkb.imports import _no_symlinks

    lock = data_dir() / ".pkb-import.lock"
    _no_symlinks(lock)
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _version_records(root: Path):
    from pkb.imports import _no_symlinks

    for target in sorted(root.glob("*/research/import-*.md")):
        _no_symlinks(target)
        metadata, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        if "import_document_id" in metadata or "import_version" in metadata:
            validate_identity(metadata.get("import_document_id"), metadata.get("import_version"))
            yield target, metadata


def validate_slot(root: Path, target: Path, document_id: str, version: int) -> None:
    category = target.relative_to(root).parts[0]
    for existing, metadata in _version_records(root):
        if metadata["import_document_id"] == document_id and existing.relative_to(root).parts[0] != category:
            raise ValueError("같은 문서ID의 버전은 같은 카테고리에 있어야 합니다.")
        slot = metadata["import_document_id"], metadata["import_version"]
        if existing != target and slot == (document_id, version):
            raise ValueError("같은 문서ID·버전에 다른 가져오기 문서가 있습니다. 덮어쓰지 않습니다.")


def _atomic_write(target: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pkb-import-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate_import_versions(assignments: list[dict], dry_run: bool = True) -> dict:
    """Preflight explicit evidence-path → UUID/version assignments before any write.

    Dry runs are read-only. Actual runs save every original card before changing any,
    preserve attachments and evidence paths, and use normal delta ingestion. A changed
    internal record seal can require reembedding its chunk. Repeating a completed or
    interrupted plan repairs indexing without changing already migrated source bytes.
    """
    if dry_run:
        return _migrate(assignments, dry_run=True)
    with import_lock():
        return _migrate(assignments, dry_run=False)


def _migrate(assignments: list[dict], *, dry_run: bool) -> dict:
    from pkb.attachments import _resolve
    from pkb.imports import _SEAL, MAX_IMPORT_BYTES, _extract, _load_record, _no_symlinks
    from pkb.operations import resolve_document_policy

    root = data_dir()
    occupied = {}
    categories = {}
    for target, metadata in _version_records(root):
        slot = metadata["import_document_id"], metadata["import_version"]
        if slot in occupied:
            raise ValueError("기존 문서ID·버전이 중복되었습니다.")
        occupied[slot] = target
        category = target.relative_to(root).parts[0]
        if slot[0] in categories and categories[slot[0]] != category:
            raise ValueError("같은 문서ID의 버전은 같은 카테고리에 있어야 합니다.")
        categories[slot[0]] = category
    seen = set()
    prepared = []
    for assignment in assignments:
        doc_id, identity, version = (assignment.get(key) for key in ("doc_id", "document_id", "version"))
        validate_identity(identity, version)
        if not isinstance(doc_id, str) or not re.fullmatch(r"data/[^/]+/research/import-[0-9a-f]{64}\.md", doc_id):
            raise ValueError("마이그레이션에는 기존 가져오기 문서 경로가 필요합니다.")
        if doc_id in seen:
            raise ValueError("마이그레이션 문서 경로가 중복되었습니다.")
        seen.add(doc_id)
        target = root / doc_id.removeprefix("data/")
        _no_symlinks(target)
        access.require("write", doc_id)
        access.require_file("write", "data/", root, target, label=doc_id)
        slot = identity, version
        if slot in occupied and occupied[slot] != target:
            raise ValueError("같은 문서ID·버전에 다른 가져오기 문서가 있습니다.")
        occupied[slot] = target
        category = target.relative_to(root).parts[0]
        if identity in categories and categories[identity] != category:
            raise ValueError("같은 문서ID의 버전은 같은 카테고리에 있어야 합니다.")
        categories[identity] = category
        raw = target.read_bytes()
        metadata, _ = parse_frontmatter(raw.decode("utf-8"))
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list) or len(attachments) != 1 or not isinstance(attachments[0], str):
            raise ValueError("원본 첨부 정보가 없습니다.")
        attachment = attachments[0]
        if not attachment.startswith("attachments/"):
            raise ValueError("원본 첨부 경로가 올바르지 않습니다.")
        attachment_root, source, _ = _resolve(attachment.removeprefix("attachments/"))
        access.require("read", attachment)
        access.require_file("read", "attachments/", attachment_root, source, label=attachment)
        _no_symlinks(source)
        expected = target.stem.removeprefix("import-")
        content, metadata = _load_record(target, attachment, expected)
        _extract(source, expected, convert=False)
        current = metadata.get("import_document_id"), metadata.get("import_version")
        if current != (None, None) and current != slot:
            raise ValueError("이미 지정된 문서ID·버전은 변경할 수 없습니다.")
        changed = current != slot
        replacement = raw
        if changed:
            # Insert only new frontmatter fields; preserve every existing YAML and body byte.
            unsigned = content[:_SEAL.search(content).start()]
            ending = "\r\n" if unsigned.startswith("---\r\n") else "\n"
            offset = len("---" + ending)
            unsigned = (unsigned[:offset] + f"import_document_id: {identity}{ending}"
                        + f"import_version: {version}{ending}" + unsigned[offset:])
            digest = hashlib.sha256(unsigned.encode()).hexdigest()
            replacement = (unsigned + f"\n<!-- pkb-import-record-sha256: {digest} -->\n").encode()
            if len(replacement) > MAX_IMPORT_BYTES:
                raise ValueError("가져오기 문서가 크기 한도를 넘습니다.")
            resolve_document_policy(doc_id, replacement.decode(), strict=True)
        backup = root / "_archive" / "import-versions" / doc_id.removeprefix("data/")
        _no_symlinks(backup)
        if backup.exists():
            old = backup.read_bytes()
            # A prior attempt must contain the exact pre-migration source.
            if changed and old != raw:
                raise ValueError("마이그레이션 백업이 기존 원문과 다릅니다.")
            if not changed:
                old_meta, _ = parse_frontmatter(old.decode())
                if "import_document_id" in old_meta or "import_version" in old_meta:
                    raise ValueError("마이그레이션 백업이 이전 형식이 아닙니다.")
                _load_record(backup, attachment, expected)
        elif not changed:
            raise ValueError("이미 마이그레이션된 문서의 원본 백업이 없습니다.")
        prepared.append((target, raw, replacement, backup, {
            "doc_id": doc_id, "document_id": identity, "version": version,
            "attachment_path": attachment, "sha256": expected, "metadata_changed": changed,
            "backup_path": str(backup), "before_sha256": hashlib.sha256(raw).hexdigest(),
            "after_sha256": hashlib.sha256(replacement).hexdigest(),
        }))
    plan = {"dry_run": dry_run, "documents": [entry[4] for entry in prepared]}
    if dry_run:
        return plan
    # Back up the whole plan first. Failed preflight never leaves a partial migration.
    for _, raw, _, backup, entry in prepared:
        if entry["metadata_changed"] and not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            _no_symlinks(backup)
            fd, temporary = tempfile.mkstemp(prefix=".pkb-import-", dir=backup.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, backup)
            finally:
                os.unlink(temporary)
    for target, raw, replacement, _, entry in prepared:
        _no_symlinks(target)
        if target.read_bytes() != raw:
            raise ValueError("마이그레이션 도중 문서가 변경되었습니다.")
        if entry["metadata_changed"]:
            _atomic_write(target, replacement)
    if prepared:
        plan["indexing"] = ingest_files([entry[0] for entry in prepared], base_dir=root, doc_id_prefix="data/")
        if plan["indexing"].get("files") != len(prepared):
            raise ValueError("마이그레이션 문서 색인이 확인되지 않았습니다. 같은 계획으로 다시 시도하세요.")
    return plan
