"""CLI와 MCP 표면이 공유하는 쓰기·변환·동기화 도메인 작업."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch


class OperationError(ValueError):
    """사용자 입력이나 원본 상태 때문에 작업을 수행할 수 없을 때 발생."""


class DocumentPolicyError(OperationError):
    """문서 경로 또는 frontmatter 정책을 위반한 쓰기."""


class OptimisticLockError(OperationError):
    """다른 프로세스가 파일을 수정한 뒤 쓰기를 시도한 경우."""


class CanonicalIdConflictError(DocumentPolicyError):
    """canonical_id가 다른 원본 Markdown 파일과 충돌하는 경우."""


class TranscriptionRequiredError(OperationError):
    """자동 텍스트 추출 대신 에이전트 전사가 필요한 입력."""

    def __init__(self, message: str, provenance: str):
        super().__init__(message)
        self.provenance = provenance


@dataclass(frozen=True)
class WriteResult:
    file_path: str
    full_path: Path
    chars: int
    stats: dict | None
    warnings: tuple[str, ...]
    # The fields below are optional extensions.  Keeping defaults preserves the
    # five-field constructor used by older CLI/MCP callers.
    changed: bool = True
    dry_run: bool = False
    diff: str = ""
    previous_hash: str | None = None
    content_hash: str | None = None
    document_type: str | None = None


@dataclass(frozen=True)
class DocumentPolicy:
    """Path-derived write policy and frontmatter diagnostics.

    ``curated`` is deliberately separate from ``document_type``.  A legacy
    path can still be classified as a regular note, but strict validation only
    applies to the explicitly mapped curated directories.  ``errors`` are
    surfaced as warnings in compatibility mode and become exceptions when
    ``strict=True`` is requested by :func:`write_and_ingest`.
    """

    file_path: str
    full_path: Path
    document_type: str
    metadata: dict[str, Any]
    curated: bool
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    canonical_id: str | None = None
    conflicts: tuple[Path, ...] = ()

    @property
    def doc_type(self) -> str:
        """Alias used by callers that name the frontmatter field ``doc_type``."""
        return self.document_type

    @property
    def valid(self) -> bool:
        return not self.errors and not self.conflicts


@dataclass(frozen=True)
class ConversionResult:
    output_path: Path
    chars: int
    stats: dict | None


@dataclass(frozen=True)
class SyncResult:
    root: Path
    stats: dict
    stale: tuple[str, ...]


_CURATED_DOCUMENT_TYPES = frozenset({"concept", "guide", "research", "moc"})
_ALLOWED_STATUS = frozenset({"canonical", "draft", "active", "superseded"})
_ALLOWED_AUTHORITY = frozenset({"curated", "researched", "imported", "generated"})
_REQUIRED_CURATED_METADATA = (
    "schema_version",
    "title",
    "doc_type",
    "canonical_id",
    "status",
    "authority",
    "tags",
)
_RESERVED_PATH_PARTS = frozenset({"_origin", "_archive"})
# 레거시 인박스: 신규 저장은 strict 모드에서 거부, 기존 파일 편집은 허용.
_LEGACY_INBOX_DIRS = frozenset({"daily-research"})

# Public read-only views for MCP/CLI surfaces and migration tooling.
CURATED_DOCUMENT_TYPES = _CURATED_DOCUMENT_TYPES
ALLOWED_STATUS = _ALLOWED_STATUS
ALLOWED_AUTHORITY = _ALLOWED_AUTHORITY
REQUIRED_CURATED_METADATA = _REQUIRED_CURATED_METADATA
RESERVED_PATH_PARTS = _RESERVED_PATH_PARTS


def content_hash(content: str) -> str:
    """Return the SHA-256 of a complete UTF-8 file payload.

    Ingest's ``content_hash`` is calculated per chunk, so this helper is kept
    in the operations layer for whole-file optimistic locking.  It intentionally
    hashes the exact bytes that ``Path.write_text(..., encoding='utf-8')``
    writes; callers should not normalize newlines before passing ``content``.
    """

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Friendly aliases for callers that use the verb-first spelling.
hash_content = content_hash
file_content_hash = content_hash


def _normalised_relative_parts(file_path: str | Path, full_path: Path) -> tuple[str, ...]:
    """Return path parts relative to DATA_ROOT where possible."""

    from pkb.config import data_dir

    try:
        return tuple(part.lower() for part in full_path.relative_to(data_dir()).parts)
    except ValueError:
        # ``resolve_data_path`` already rejects this for writes, but retaining a
        # useful fallback keeps ``derive_document_type`` reusable for diagnostics
        # on an arbitrary path.
        parts = tuple(part.lower() for part in Path(file_path).parts)
        if parts and parts[0] == "data":
            return parts[1:]
        return parts


def derive_document_type(file_path: str | Path) -> str:
    """Derive a curated document type from its path.

    Curated directories are intentionally narrow and opt-in:

    * any ``concepts/`` segment → ``concept``
    * any ``guides/`` segment → ``guide``
    * any ``research/`` segment → ``research``
    * basename ``00_MOC.md`` → ``moc``

    ``_origin`` and ``_archive`` are excluded/reserved and therefore return
    ``legacy`` even if a parent segment looks curated.  All other paths return
    ``note``; this is the compatibility path for the existing dynamic category
    layout.
    """

    from pkb.documents import resolve_data_path

    path = Path(file_path)
    full_path = resolve_data_path(str(file_path))
    if full_path is None:
        full_path = path.expanduser().resolve() if path.is_absolute() else path
    parts = _normalised_relative_parts(file_path, full_path)
    if any(part in _RESERVED_PATH_PARTS for part in parts):
        return "legacy"
    if parts and parts[-1] == "00_moc.md":
        return "moc"
    if "concepts" in parts[:-1]:
        return "concept"
    if "guides" in parts[:-1]:
        return "guide"
    if "research" in parts[:-1]:
        return "research"
    return "note"


# Alternate names keep the policy discoverable without forcing a naming change
# on callers that already use one of these spellings.
document_type_from_path = derive_document_type
path_document_type = derive_document_type


def _is_nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_nonempty_tags(value: object) -> bool:
    if isinstance(value, str):
        return any(item.strip() for item in value.split(","))
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _parse_frontmatter(content: str) -> dict[str, Any]:
    from pkb.ingest import parse_frontmatter

    metadata, _ = parse_frontmatter(content)
    return metadata if isinstance(metadata, dict) else {}


def _is_reserved_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part.lower() in _RESERVED_PATH_PARTS for part in parts)


def _canonical_id_conflicts(
    root: Path, target: Path, canonical_id: str
) -> tuple[Path, ...]:
    """Find other Markdown files carrying the same canonical_id.

    The scan deliberately uses only the filesystem and ``parse_frontmatter``;
    no Elasticsearch/graph coupling is needed for a pre-write safety check.
    Reserved import/archive trees are skipped because they are not curated
    identities and can legitimately retain source metadata.
    """

    if not root.exists() or not canonical_id:
        return ()
    conflicts: list[Path] = []
    target_exists = target.exists()
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in {".md", ".markdown"}:
            continue
        # Path 문자열 비교는 한글 파일명의 NFC/NFD 차이로 자기 자신을 "다른 문서"로 오인한다
        # (APFS는 조회 시 정규화 무시, 보존 시 원형 유지). inode 비교(samefile)로 같은 파일을 건너뛴다.
        if (target_exists and candidate.samefile(target)) or _is_reserved_path(candidate, root):
            continue
        try:
            metadata = _parse_frontmatter(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        value = metadata.get("canonical_id")
        if isinstance(value, str) and value.strip() == canonical_id:
            conflicts.append(candidate.resolve())
    return tuple(sorted(conflicts))


def resolve_document_policy(
    file_path: str | Path,
    content: str | None = None,
    *,
    strict: bool = False,
) -> DocumentPolicy:
    """Resolve path-derived type and validate curated frontmatter metadata.

    The function never writes.  ``strict`` controls only whether diagnostics
    are raised immediately; :func:`write_and_ingest` uses the same diagnostics
    as warning text when compatibility mode is requested.
    """

    from pkb.config import data_dir
    from pkb.documents import resolve_data_path

    full_path = resolve_data_path(str(file_path))
    if full_path is None:
        raise DocumentPolicyError(
            f"data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
        )
    root = data_dir()
    document_type = derive_document_type(file_path)
    curated = document_type in _CURATED_DOCUMENT_TYPES
    # 레거시 인박스 신규 저장 차단.  strict는 원래 curated 경로에만 적용되지만,
    # 인박스에 새 문서가 계속 쌓이는 것은 마이그레이션 자체를 되돌리므로 예외로 막는다.
    # 기존 파일 편집(아카이브 frontmatter, 이관 등)은 그대로 허용.
    if strict and not full_path.exists():
        parts = _normalised_relative_parts(file_path, full_path)
        if parts and parts[0] in _LEGACY_INBOX_DIRS:
            raise DocumentPolicyError(
                f"{parts[0]}/는 레거시 인박스라 새 문서를 만들 수 없습니다. "
                "list_documents(limit=0)로 기존 주제 폴더를 확인한 뒤 "
                "<주제>/research/ 같은 curated 경로에 frontmatter"
                "(schema_version, title, doc_type, canonical_id, status, authority, tags)를 "
                "포함해 저장하세요. 기존 파일 편집은 허용됩니다."
            )
    if content is None:
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise DocumentPolicyError(f"기존 파일을 읽을 수 없습니다: {full_path}") from exc
        else:
            content = ""
    metadata = _parse_frontmatter(content)

    required = _REQUIRED_CURATED_METADATA if curated else ()
    missing: list[str] = [field for field in required if field not in metadata]
    errors: list[str] = []
    if curated:
        if missing:
            errors.append("필수 frontmatter 누락: " + ", ".join(missing))
        if "schema_version" in metadata and metadata["schema_version"] in (None, ""):
            missing.append("schema_version")
            errors.append("schema_version은 비어 있지 않아야 합니다")
        if "title" in metadata and not _is_nonblank(metadata["title"]):
            errors.append("title은 비어 있지 않은 문자열이어야 합니다")
        doc_type_value = metadata.get("doc_type")
        if "doc_type" in metadata:
            if not _is_nonblank(doc_type_value):
                errors.append("doc_type은 비어 있지 않은 문자열이어야 합니다")
            elif str(doc_type_value).strip().lower() != document_type:
                errors.append(
                    f"doc_type이 경로와 다릅니다: 경로={document_type!r}, "
                    f"frontmatter={doc_type_value!r}"
                )
        canonical = metadata.get("canonical_id")
        if "canonical_id" in metadata and not _is_nonblank(canonical):
            errors.append("canonical_id는 비어 있지 않은 문자열이어야 합니다")
        status = metadata.get("status")
        if "status" in metadata and status not in _ALLOWED_STATUS:
            errors.append(
                f"status는 다음 중 하나여야 합니다: {', '.join(sorted(_ALLOWED_STATUS))}"
            )
        authority = metadata.get("authority")
        if "authority" in metadata and authority not in _ALLOWED_AUTHORITY:
            errors.append(
                "authority는 다음 중 하나여야 합니다: "
                + ", ".join(sorted(_ALLOWED_AUTHORITY))
            )
        if "tags" in metadata and not _is_nonempty_tags(metadata["tags"]):
            errors.append("tags는 비어 있지 않은 문자열 또는 문자열 리스트여야 합니다")

    canonical_id = metadata.get("canonical_id")
    canonical_id = canonical_id.strip() if isinstance(canonical_id, str) else None
    conflicts = (
        _canonical_id_conflicts(root, full_path, canonical_id)
        if canonical_id
        else ()
    )
    if conflicts:
        errors.append(
            "canonical_id가 다른 문서와 충돌합니다: "
            + ", ".join(str(path) for path in conflicts)
        )

    # Keep the old warning-only behavior for non-curated paths.  For curated
    # paths in compatibility mode, policy errors are also warnings so callers
    # can preview and repair without losing the existing write behavior.
    warnings = list(frontmatter_warnings(content))
    warnings.extend(errors)
    # ``strict`` is intentionally scoped to curated path mappings.  Legacy
    # category paths must remain writable (with diagnostics) during migration,
    # even when the caller enables strict mode for curated writes.
    if strict and curated and errors:
        message = "문서 정책 위반: " + "; ".join(errors)
        if conflicts:
            raise CanonicalIdConflictError(message)
        raise DocumentPolicyError(message)
    return DocumentPolicy(
        file_path=str(file_path),
        full_path=full_path,
        document_type=document_type,
        metadata=metadata,
        curated=curated,
        required_fields=tuple(required),
        missing_fields=tuple(dict.fromkeys(missing)),
        errors=tuple(errors),
        warnings=tuple(dict.fromkeys(warnings)),
        canonical_id=canonical_id,
        conflicts=conflicts,
    )


def document_policy(
    file_path: str | Path,
    content: str | None = None,
    *,
    strict: bool = False,
) -> DocumentPolicy:
    """Short alias for :func:`resolve_document_policy`."""

    return resolve_document_policy(file_path, content, strict=strict)


# Public verb-oriented aliases.  Keeping these tiny wrappers avoids callers
# depending on the internal ``_canonical_id_conflicts`` scanner while retaining
# one implementation of policy resolution.
validate_document_policy = resolve_document_policy
validate_document_metadata = resolve_document_policy


def _write_diff(file_path: str | Path, before: str, after: str) -> str:
    """Build a deterministic unified diff for dry-run write previews."""

    if before == after:
        return ""
    # ``splitlines(keepends=True)`` preserves exact line endings and makes a
    # preview faithful to the bytes that would be written.
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{file_path} (before)",
            tofile=f"{file_path} (after)",
            lineterm="\n",
        )
    )


def preview_write(
    file_path: str,
    content: str,
    *,
    expected_hash: str = "",
    strict_policy: bool = False,
) -> WriteResult:
    """Return the same result as a dry-run write without touching disk/ES."""

    return write_and_ingest(
        file_path,
        content,
        ingest=False,
        dry_run=True,
        expected_hash=expected_hash,
        strict_policy=strict_policy,
    )


def frontmatter_warnings(content: str) -> tuple[str, ...]:
    """저장 전 frontmatter 품질 경고. 작업 자체는 막지 않는다."""
    from pkb.ingest import parse_expires_at, parse_frontmatter

    fm, _ = parse_frontmatter(content)
    if not fm:
        return ("frontmatter 없음 또는 YAML 파싱 실패 — title·tags를 담은 '---' 블록 권장",)
    warnings: list[str] = []
    title = fm.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        warnings.append(f"title이 비문자열이거나 공백입니다: {title!r}")
    tags = fm.get("tags")
    if tags is not None and not (
        isinstance(tags, str)
        or (isinstance(tags, list) and all(isinstance(tag, str) for tag in tags))
    ):
        warnings.append(f"tags는 문자열(쉼표 구분) 또는 문자열 리스트여야 합니다: {tags!r}")
    if "expires_at" in fm and parse_expires_at(fm["expires_at"]) is None:
        warnings.append(f"expires_at 파싱 실패 (ISO8601 필요): {fm['expires_at']!r}")
    return tuple(warnings)


def write_and_ingest(
    file_path: str,
    content: str,
    *,
    ingest: bool = True,
    dry_run: bool = False,
    expected_hash: str = "",
    strict_policy: bool = False,
) -> WriteResult:
    """Write a Markdown document and optionally ingest it.

    ``ingest=True`` remains the historical default for CLI callers.  MCP can
    opt into ``ingest=False`` and a stricter curated-document policy without
    changing the operation's compatibility behavior.

    ``dry_run`` computes policy diagnostics and a unified diff but never creates
    directories, writes the file, or calls Elasticsearch ingestion.  If
    ``expected_hash`` is supplied, it must match the SHA-256 of the current
    complete file payload (and a missing file never matches).
    """
    from pkb.config import data_dir
    from pkb.documents import resolve_data_path

    # The historical surface documented a string, but accepting Path-like
    # values is harmless and keeps CLI/automation callers ergonomic.
    file_path = str(file_path)
    full_path = resolve_data_path(file_path)
    if full_path is None:
        raise OperationError(
            f"data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
        )
    if full_path.suffix != ".md":
        raise OperationError(f"마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})")

    previous_content = ""
    previous_hash: str | None = None
    existed = full_path.exists()
    if existed:
        try:
            previous_content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise OperationError(f"기존 파일을 읽을 수 없습니다: {full_path}") from exc
        previous_hash = content_hash(previous_content)

    expected = str(expected_hash or "").strip()
    if expected.lower().startswith("sha256:"):
        expected = expected.split(":", 1)[1].strip()
    if expected and (previous_hash is None or expected.lower() != previous_hash.lower()):
        actual = previous_hash or "<missing>"
        raise OptimisticLockError(
            f"optimistic lock 실패: expected_hash={expected}, 현재 파일 hash={actual}"
        )

    policy = resolve_document_policy(file_path, content, strict=strict_policy)
    diff = _write_diff(file_path, previous_content, content)
    changed = not existed or previous_content != content

    if dry_run:
        return WriteResult(
            file_path=file_path,
            full_path=full_path,
            chars=len(content),
            stats=None,
            warnings=policy.warnings,
            changed=changed,
            dry_run=True,
            diff=diff,
            previous_hash=previous_hash,
            content_hash=content_hash(content),
            document_type=policy.document_type,
        )

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")

    stats = None
    if ingest:
        from pkb.ingest import ingest_files

        stats = ingest_files([full_path], base_dir=data_dir(), doc_id_prefix="data/")
    return WriteResult(
        file_path=file_path,
        full_path=full_path,
        chars=len(content),
        stats=stats,
        warnings=policy.warnings,
        changed=changed,
        dry_run=False,
        diff=diff,
        previous_hash=previous_hash,
        content_hash=content_hash(content),
        document_type=policy.document_type,
    )


def convert_and_ingest(
    input_path: str | Path,
    *,
    category: str,
    output: Path | None = None,
    output_name: str = "",
    ingest: bool = True,
) -> ConversionResult:
    """지원 문서를 Markdown으로 변환해 data/ 아래 저장하고 선택적으로 인제스트."""
    from pkb.config import data_dir
    from pkb.ingest import (
        SUPPORTED_EXTENSIONS,
        conversion_frontmatter,
        ingest_files,
        read_file_as_text,
    )

    source = Path(input_path).expanduser().resolve()
    if not source.exists():
        raise OperationError(f"파일을 찾을 수 없습니다: {input_path}")

    provenance = conversion_frontmatter(source)
    suffix = source.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise TranscriptionRequiredError("이미지는 자동 변환할 수 없습니다.", provenance)
    if suffix not in SUPPORTED_EXTENSIONS:
        raise OperationError(
            f"지원하지 않는 형식입니다: {source.suffix} (지원: {sorted(SUPPORTED_EXTENSIONS)})"
        )
    if any(part.startswith(".") for part in Path(category).parts):
        raise OperationError(f"숨김 폴더에는 저장할 수 없습니다. (입력: {category})")

    try:
        text = read_file_as_text(source)
    except Exception as exc:
        raise OperationError(f"변환 실패: {input_path} — {exc}") from exc
    if not text.strip():
        raise TranscriptionRequiredError(
            f"텍스트를 추출할 수 없습니다 (스캔 PDF 등): {input_path}", provenance
        )

    root = data_dir()
    if output is None:
        stem = output_name or source.stem
        output_path = (root / category / f"{stem}.md").resolve()
    else:
        output_path = output.expanduser().resolve()
    if not output_path.is_relative_to(root):
        raise OperationError(f"출력 경로는 data 코퍼스({root}) 하위여야 합니다: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(provenance + text, encoding="utf-8")
    stats = None
    if ingest:
        stats = ingest_files([output_path], base_dir=root, doc_id_prefix="data/")
    return ConversionResult(output_path=output_path, chars=len(text), stats=stats)


def sync_tree(
    es: Elasticsearch,
    root: Path,
    prefix: str,
    *,
    exclude: Path | None = None,
) -> SyncResult:
    """한 원본 트리를 ES와 재조정하고 삭제 후보를 반환."""
    from pkb.ingest import reconcile

    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise OperationError(f"디렉터리를 찾을 수 없습니다 (정리하지 않음): {resolved}")
    stats, stale = reconcile(es, resolved, prefix, exclude=exclude)
    return SyncResult(resolved, stats, tuple(stale))


def prune_documents(es: Elasticsearch, doc_ids: list[str] | tuple[str, ...]) -> int:
    """확인 완료된 stale 문서를 삭제."""
    from pkb.store import delete_document

    for doc_id in doc_ids:
        delete_document(es, doc_id)
    return len(doc_ids)


def graph_prune_summary(es: Elasticsearch) -> str:
    """ES에 없는 문서의 그래프 파생 데이터를 정리하고 요약."""
    from pathlib import Path

    from pkb.config import settings
    from pkb.graph import store as graph_store
    from pkb.graph.schema import graph_connection
    from pkb.store import list_doc_ids

    if not Path(settings.graph_db_path).exists():
        return ""
    existing = list_doc_ids(es, "data/") | list_doc_ids(es, "obsidian/")
    with graph_connection(settings.graph_db_path) as conn:
        result = graph_store.prune_missing_documents(conn, existing)
    if not result["mentions_pruned"] and not result["documents_pruned"]:
        return ""
    return (
        f"그래프 정리: mentions {result['mentions_pruned']}·"
        f"documents {result['documents_pruned']}"
    )
