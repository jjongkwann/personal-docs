"""CLI와 MCP 표면이 공유하는 쓰기·변환·동기화 도메인 작업."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from elasticsearch import Elasticsearch


class OperationError(ValueError):
    """사용자 입력이나 원본 상태 때문에 작업을 수행할 수 없을 때 발생."""


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


def write_and_ingest(file_path: str, content: str, *, ingest: bool = True) -> WriteResult:
    """data/ 하위 Markdown을 저장하고 선택적으로 인제스트."""
    from pkb.config import data_dir
    from pkb.documents import resolve_data_path

    full_path = resolve_data_path(file_path)
    if full_path is None:
        raise OperationError(
            f"data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
        )
    if full_path.suffix != ".md":
        raise OperationError(f"마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})")

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
        warnings=frontmatter_warnings(content),
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
