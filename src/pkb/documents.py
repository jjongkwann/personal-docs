"""CLI와 MCP가 공유하는 문서 경로·조회·생명주기 코어."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from elasticsearch import Elasticsearch


class DocumentPathError(ValueError):
    """doc_id가 설정된 코퍼스/볼트 경계를 벗어날 때 발생."""


@dataclass(frozen=True)
class LifecycleResult:
    message: str
    found: bool = True


@dataclass(frozen=True)
class ReindexTarget:
    file_path: Path
    base_dir: Path
    doc_id_prefix: str


def resolve_data_path(file_path: str) -> Path | None:
    """`data/...` doc_id 또는 코퍼스 하위 절대경로를 실제 경로로 변환."""
    from pkb.config import data_dir

    root = data_dir()
    path = Path(file_path)
    if path.is_absolute():
        full = path.resolve()
    elif file_path == "data" or file_path.startswith("data/"):
        full = (root / file_path.removeprefix("data/").lstrip("/")).resolve()
    else:
        return None
    return full if full.is_relative_to(root) else None


def strip_archive_frontmatter(text: str) -> str:
    """frontmatter 블록에서 archived_at/archive_reason 줄만 제거."""
    match = re.match(r"^(---\r?\n)(.*?\r?\n)(---\r?\n)", text, re.DOTALL)
    if not match:
        return text
    body = re.sub(
        r"^archived_at:[^\n]*\n|^archive_reason:[^\n]*\n",
        "",
        match.group(2),
        flags=re.MULTILINE,
    )
    if not body.strip():
        return text[match.end(3):]
    return match.group(1) + body + text[match.start(3):]


def insert_archive_frontmatter(text: str, archived_at: str, reason: str) -> str:
    """frontmatter에 archived_at과 선택적 archive_reason을 텍스트로 삽입."""
    lines = f"archived_at: {archived_at}\n"
    if reason:
        lines += f"archive_reason: {json.dumps(reason, ensure_ascii=False)}\n"
    if text and not text.endswith("\n"):
        text += "\n"
    text = strip_archive_frontmatter(text)
    match = re.match(r"^---\r?\n", text)
    if match:
        return text[: match.end()] + lines + text[match.end():]
    return f"---\n{lines}---\n{text}"


def archive_document(doc_id: str, reason: str = "") -> LifecycleResult:
    """원본 frontmatter를 SSOT로 아카이브하고, 불가능한 형식은 ES에 기록."""
    from pkb.store import archive_document as archive_in_es
    from pkb.store import get_client

    full_path = resolve_data_path(doc_id) if doc_id.endswith(".md") else None
    if full_path is not None and full_path.exists():
        from pkb.config import data_dir
        from pkb.ingest import format_delta_stats, ingest_files, parse_frontmatter
        from pkb.search_log import log_change

        text = insert_archive_frontmatter(
            full_path.read_text(encoding="utf-8"), datetime.now(UTC).isoformat(), reason
        )
        frontmatter, _ = parse_frontmatter(text)
        if frontmatter.get("archived_at"):
            full_path.write_text(text, encoding="utf-8")
            stats = ingest_files([full_path], base_dir=data_dir(), doc_id_prefix="data/")
            log_change("archive", doc_id, reason=reason or None)
            message = f"아카이브 완료: {doc_id} (frontmatter 기록 — {format_delta_stats(stats)})"
            if reason:
                message += f" | 사유: {reason}"
            return LifecycleResult(message)

    count = archive_in_es(get_client(), doc_id, reason=reason or None)
    if count == 0:
        return LifecycleResult(f"아카이브 대상 없음 (doc_id={doc_id})", found=False)
    message = f"아카이브 완료: {doc_id} ({count}개 청크)"
    if reason:
        message += f" | 사유: {reason}"
    return LifecycleResult(message)


def restore_document(doc_id: str) -> LifecycleResult:
    """원본 frontmatter의 아카이브 상태와 ES 필드를 함께 제거."""
    from pkb.store import get_client
    from pkb.store import restore_document as restore_in_es

    full_path = resolve_data_path(doc_id) if doc_id.endswith(".md") else None
    if full_path is not None and full_path.exists():
        text = full_path.read_text(encoding="utf-8")
        stripped = strip_archive_frontmatter(text)
        if stripped != text:
            from pkb.config import data_dir
            from pkb.ingest import format_delta_stats, ingest_files

            full_path.write_text(stripped, encoding="utf-8")
            stats = ingest_files([full_path], base_dir=data_dir(), doc_id_prefix="data/")
            restore_in_es(get_client(), doc_id)
            return LifecycleResult(
                f"복구 완료: {doc_id} (frontmatter 제거 — {format_delta_stats(stats)})"
            )

    count = restore_in_es(get_client(), doc_id)
    if count == 0:
        return LifecycleResult(f"복구할 아카이브 없음 (doc_id={doc_id})", found=False)
    return LifecycleResult(f"복구 완료: {doc_id} ({count}개 청크)")


def fetch_document_sources(
    es: Elasticsearch, doc_id: str, *, limit: int = 500
) -> list[dict]:
    """doc_id의 청크를 순서대로 읽고 embedding을 제외한 source 목록을 반환."""
    from pkb.config import settings

    result = es.search(
        index=settings.es_index,
        query={"term": {"doc_id": doc_id}},
        size=limit,
        source_excludes=["embedding"],
        sort=[{"chunk_index": {"order": "asc"}}],
    )
    return [hit["_source"] for hit in result["hits"]["hits"]]


def resolve_reindex_target(doc_id: str) -> ReindexTarget:
    """data/ 또는 obsidian/ doc_id를 경계 검증된 원본 경로로 변환."""
    from pkb.config import data_dir, settings

    if doc_id.startswith("obsidian/"):
        if not settings.obsidian_path:
            raise DocumentPathError("OBSIDIAN_PATH가 설정되지 않았습니다.")
        base_dir = Path(settings.obsidian_path).expanduser().resolve()
        file_path = (base_dir / doc_id.removeprefix("obsidian/")).resolve()
        if not file_path.is_relative_to(base_dir):
            raise DocumentPathError(f"obsidian/ 하위 경로만 허용됩니다: {doc_id}")
        return ReindexTarget(file_path, base_dir, "obsidian/")

    file_path = resolve_data_path(doc_id)
    if file_path is None:
        raise DocumentPathError(
            f"알 수 없는 doc_id 형식입니다 (data/ 또는 obsidian/ 접두사 필요): {doc_id}"
        )
    return ReindexTarget(file_path, data_dir(), "data/")


def parse_chunk_range(chunk_range: str) -> tuple[int, int] | None:
    """`3` 또는 `3-7`을 inclusive 청크 범위로 파싱."""
    value = chunk_range.strip()
    if "-" in value:
        left, _, right = value.partition("-")
        try:
            start, end = int(left), int(right)
        except ValueError:
            return None
    else:
        try:
            start = end = int(value)
        except ValueError:
            return None
    if start < 0 or end < start:
        return None
    return start, end


def render_document(
    doc_id: str, sources: list[dict], include_content: bool, chunk_range: str
) -> str:
    """문서 청크 목록을 MCP/CLI 공용 텍스트 표현으로 렌더."""
    header = (
        f"문서: {doc_id} ({len(sources)}개 청크)\n"
        f"제목: {sources[0].get('title') or '-'} | 카테고리: {sources[0].get('category')} | "
        f"수정일: {sources[0].get('date_modified', '-')}"
    )

    def full(selected: list[dict]) -> str:
        lines = [header]
        for source in selected:
            section = source.get("section_path") or "(섹션 없음)"
            lines.append(
                f"\n--- 청크 #{source['chunk_index']} | {section} ---\n{source.get('content', '')}"
            )
        return "\n".join(lines)

    if chunk_range:
        parsed = parse_chunk_range(chunk_range)
        if parsed is None:
            return f"오류: chunk_range 형식이 잘못됐습니다: {chunk_range!r} ('3' 또는 '3-7')"
        start, end = parsed
        selected = [source for source in sources if start <= source["chunk_index"] <= end]
        if not selected:
            return f"오류: 범위 {chunk_range}에 해당하는 청크가 없습니다."
        return full(selected)
    if include_content:
        return full(sources)

    lines = [header, "\n청크 목차:"]
    for source in sources:
        section = source.get("section_path") or "(섹션 없음)"
        content = (source.get("content") or "").replace("\n", " ").strip()
        preview = content[:120] + ("…" if len(content) > 120 else "")
        lines.append(f"- #{source['chunk_index']} {section}: {preview}")
    lines.append("\n전문이 필요하면 include_content=True 또는 chunk_range='0-3'을 사용하세요.")
    return "\n".join(lines)
