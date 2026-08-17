import codecs
import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import tiktoken
import yaml

from pkb.config import settings

_log = logging.getLogger(__name__)

# 인제스트 가능한 포맷 — 원본 마크다운/텍스트 + 변환 대상(pdf는 pdfminer, 나머지는 markitdown)
SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".txt",
    ".pdf", ".docx", ".pptx", ".xlsx",
    ".html", ".htm",
}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}

_encoder: tiktoken.Encoding | None = None
_markitdown = None


def _get_markitdown():
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown

        _markitdown = MarkItDown()
    return _markitdown


def _pdf_to_markdown(file_path: Path) -> str:
    """PDF를 페이지 마커("## p.N") 붙은 마크다운으로 변환.

    extract_pages 단일 패스로 페이지별 텍스트를 모은다 — 페이지별 extract_text
    반복 호출은 매번 전체 재파싱(O(n²))이라 금지. 마커는 기존 헤딩 청커가
    section_path="p.N"으로 자동 승격한다. 텍스트 없는 페이지(이미지 전용 등)는 건너뛴다.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer

    pages: list[str] = []
    for layout in extract_pages(str(file_path)):
        pages.append(
            "".join(el.get_text() for el in layout if isinstance(el, LTTextContainer)).strip()
        )
    return _join_pdf_pages(pages)


def _join_pdf_pages(pages: list[str]) -> str:
    """페이지 텍스트 리스트를 "## p.N" 마커로 연결하는 순수 함수. 빈 페이지는 번호만 소비."""
    return "\n\n".join(
        f"## p.{n}\n\n{text}" for n, text in enumerate(pages, 1) if text
    )


def read_file_as_text(file_path: Path) -> str:
    """파일을 텍스트로 변환. md/txt는 그대로, pdf는 pdfminer 페이지 보존 추출,
    docx/pptx/xlsx/html은 markitdown 사용.

    PDF에 markitdown을 쓰지 않는 이유: markitdown의 PDF 변환도 결국 pdfminer 텍스트
    추출이라 품질은 동일한데 페이지 경계가 소실된다 — 분기 하나로 페이지 정보를 지킨다.
    """
    ext = file_path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return file_path.read_text(encoding="utf-8")
    if ext == ".pdf":
        return _pdf_to_markdown(file_path)
    result = _get_markitdown().convert(str(file_path))
    return result.text_content


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 임베딩 입력 구성 로직 버전. embed_input의 조합 방식이 바뀌면 올린다 —
# fingerprint가 전부 달라져 다음 sync 때 전량 재임베딩된다.
EMBED_PREPROC_VERSION = "1"


def embed_input(chunk: dict) -> str:
    """실제로 임베딩되는 텍스트. embed_context_prefix면 title·section_path를 앞에 붙인다."""
    if not settings.embed_context_prefix:
        return chunk["content"]
    prefix = "\n".join(
        p for p in (chunk.get("title") or "", chunk.get("section_path") or "") if p
    )
    return f"{prefix}\n{chunk['content']}" if prefix else chunk["content"]


def embedding_fingerprint(chunk: dict) -> str:
    """벡터 재사용 판정 키 — 모델·전처리 버전·임베딩 입력이 모두 같을 때만 재사용.

    content_hash는 '본문이 같은가'(그래프 pending 추적)만 답한다. 모델 교체나
    prefix 토글처럼 본문이 같아도 벡터가 달라져야 하는 변경은 이 키가 잡는다.
    """
    key = "\n".join([settings.embedding_model, EMBED_PREPROC_VERSION, embed_input(chunk)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


_META_DIFF_FIELDS = (
    "tags",
    "title",
    "expires_at",
    "archived_at",
    "archive_reason",
    "date_modified",
    "category",
    "section_path",
    "language",
    "source_path",
    # Optional document contract metadata.  These fields are intentionally
    # diffed alongside the historical fields above so a frontmatter-only edit
    # can use the metadata update path without re-embedding a chunk.
    "schema_version",
    "doc_type",
    "canonical_id",
    "status",
    "authority",
    "aliases",
    "source_ids",
    "supports",
    "concept_ids",
)

# Frontmatter fields introduced after the original corpus was created.  Keep
# the names in one place so ingest, metadata diffing, and tests can share the
# same contract.  Optional fields are omitted from chunks when the key is
# absent; this is important for old notes and lets ``_diff_metadata`` preserve
# values that were written by a newer producer when an old note is reindexed.
DOCUMENT_METADATA_FIELDS = (
    "schema_version",
    "doc_type",
    "canonical_id",
    "status",
    "authority",
    "aliases",
    "source_ids",
    "supports",
    "concept_ids",
)
_DOCUMENT_METADATA_LIST_FIELDS = frozenset(
    {"aliases", "source_ids", "supports", "concept_ids"}
)
_DOCUMENT_METADATA_SCALAR_FIELDS = frozenset(
    {"schema_version", "doc_type", "canonical_id", "status", "authority"}
)


def _diff_metadata(old: dict, new: dict) -> dict:
    """content 외 필드 비교. 다른 필드만 dict로 반환. tags는 정렬 후 비교."""
    diff: dict = {}
    for f in _META_DIFF_FIELDS:
        # The document-contract fields are optional.  A legacy producer may
        # omit them entirely, and an old note without frontmatter must not
        # erase values already present in ES during a metadata-only update.
        # An explicit YAML null is different: process_file includes the key
        # and the resulting ``None`` is propagated as a field removal.
        if f in DOCUMENT_METADATA_FIELDS and f not in new:
            continue
        ov, nv = old.get(f), new.get(f)
        if f == "tags" or f in _DOCUMENT_METADATA_LIST_FIELDS:
            # ES returns keyword arrays as lists, but hand-built legacy test
            # fixtures (and a few old importers) may use a scalar.  Compare
            # normalized string sets so ordering and representation do not
            # trigger a spurious metadata update.
            def _as_sorted_list(value: object) -> list[str]:
                if value is None:
                    return []
                if isinstance(value, str):
                    return [value]
                if isinstance(value, (list, tuple, set, frozenset)):
                    return sorted(str(item) for item in value if item is not None)
                return [str(value)]

            ov = _as_sorted_list(ov)
            nv = _as_sorted_list(nv)
        # 특수 케이스: 아카이브 필드는 None을 전파하지 않는다 — frontmatter 마커 없는
        # 파일(볼트 노트·비-md·ES-only 아카이브 md)의 재인제스트가 ES 아카이브 상태를
        # 지우면 안 됨. 마커 제거 복구는 restore_document가 ES 필드를 명시 해제한다.
        if f in ("archived_at", "archive_reason") and nv is None:
            continue
        if ov != nv:
            diff[f] = new.get(f)
    return diff


def _normalize_document_metadata(frontmatter: dict) -> dict:
    """Normalize optional document-contract metadata from YAML frontmatter.

    Missing keys are omitted so old documents retain their historical shape.
    Scalars remain scalars (including numeric ``schema_version`` values),
    while list-like fields are converted to a stable list of strings.  A
    comma-separated string is accepted for convenience, matching
    the existing ``tags`` frontmatter behavior.  Explicit ``null`` is kept as
    ``None`` so callers can intentionally clear a field via metadata delta.
    """
    metadata: dict = {}
    for field in DOCUMENT_METADATA_FIELDS:
        if field not in frontmatter:
            continue
        value = frontmatter[field]
        if value is None:
            metadata[field] = None
            continue

        if field in _DOCUMENT_METADATA_LIST_FIELDS:
            if isinstance(value, str):
                values = value.split(",")
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
            else:
                values = [value]
            normalized: list[str] = []
            for item in values:
                if item is None:
                    continue
                item_text = str(item).strip()
                if not item_text:
                    continue
                normalized.append(item_text)
            metadata[field] = normalized
            continue

        # Scalar fields are intentionally permissive.  YAML commonly parses
        # ``schema_version: 1`` as an integer; preserving that value keeps the
        # chunk API unsurprising while Elasticsearch's keyword mapping accepts
        # the value through its normal coercion rules.
        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    value = None
            metadata[field] = value
        else:
            metadata[field] = str(value).strip() or None
    return metadata


def format_delta_stats(stats: dict) -> str:
    return (
        f"files={stats['files']} reused={stats['reused']} "
        f"moved={stats['moved']} "
        f"embedded={stats['embedded']} added={stats['added']} "
        f"metadata_updated={stats['metadata_updated']} deleted={stats['deleted']}"
    )


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def parse_expires_at(value: object) -> str | None:
    """frontmatter의 expires_at 값을 ISO8601 문자열로 정규화.

    허용 입력:
      - date 객체 (YAML이 `2026-12-31`을 자동 date로 파싱)
      - datetime 객체
      - ISO 문자열 ('2026-12-31' 또는 '2026-12-31T10:00:00')

    실패 시 None 반환 + 경고 로그. chunk dict에는 유효한 경우만 반영.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            _log.warning("invalid expires_at (ISO parse failed): %r", value)
            return None
        return parsed.isoformat()
    _log.warning("unsupported expires_at type: %s (%r)", type(value).__name__, value)
    return None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """YAML frontmatter가 있으면 (metadata, 본문) 반환. 없으면 ({}, 원문)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
        if not isinstance(metadata, dict):
            return {}, text
        return metadata, match.group(2)
    except yaml.YAMLError:
        return {}, text


def conversion_frontmatter(src: Path) -> str:
    """변환 산출물(.md) 상단에 붙일 provenance frontmatter 블록 생성.

    title은 의도적으로 넣지 않는다 — 본문 H1 추출(_extract_title)이 원본 파일명보다
    나은 제목을 주는데, frontmatter title이 있으면 그게 우선돼 퇴행한다.
    """
    meta = {
        "source": src.name,
        "converted_from": src.suffix,
        "converted_at": date.today().isoformat(),
    }
    block = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{block}---\n\n"


def _split_by_headings_hierarchical(text: str) -> list[tuple[str, str]]:
    """H1~H3 헤딩 경계로 분할하되 section_path 동반.
    반환: [(section_path, section_text), ...]
    section_path 예: "RAG 개요 > 하이브리드 검색"
    """
    sections: list[tuple[str, str]] = []
    current: list[str] = []
    path_stack: list[tuple[int, str]] = []  # [(level, heading_text)]
    current_path = ""

    def flush():
        if current:
            sections.append((current_path, "\n".join(current)))

    in_fence = False
    for line in text.split("\n"):
        # ponytail: ``` 펜스만 인식 — ~~~ 펜스는 코퍼스에 없어 생략, 필요해지면 ~~~ 분기 추가.
        # 열림(```lang)/닫힘(```)만 매치 — 한 줄 인라인 ```...``` 은 [^`]* 가 걸러 텍스트 취급
        if re.match(r"^\s*```[^`]*$", line):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,3})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()

            flush()
            current = []

            # 스택 갱신
            path_stack = [x for x in path_stack if x[0] < level]
            path_stack.append((level, heading))
            current_path = " > ".join(h for _, h in path_stack)

        current.append(line)

    flush()
    return sections


def _split_oversized_paragraph(para: str, max_tokens: int) -> list[str]:
    """max_tokens 초과 단락을 라인 단위로 재누적 분할.

    단일 라인이 여전히 초과하면 토큰 id 슬라이스로 하드 분할.
    """
    enc = _get_encoder()
    pieces: list[str] = []
    buf: list[str] = []
    for line in para.split("\n"):
        if _count_tokens(line) > max_tokens:
            if buf:
                pieces.append("\n".join(buf))
                buf = []
            ids = enc.encode(line)
            # 토큰 경계가 멀티바이트 문자를 가르면 decode()가 U+FFFD로 원문을 파괴한다 —
            # decode_bytes + 증분 UTF-8 디코더로 잘린 바이트를 다음 조각에 이월.
            # 이월 바이트가 조각을 최대 1토큰 늘릴 수 있어 슬라이스 폭은 max_tokens - 1
            dec = codecs.getincrementaldecoder("utf-8")()
            width = max(1, max_tokens - 1)
            for i in range(0, len(ids), width):
                piece = dec.decode(enc.decode_bytes(ids[i:i + width]), i + width >= len(ids))
                if piece:
                    pieces.append(piece)
        # ponytail: 누적 후보 전체를 재인코딩(O(n²)) — 단락 규모에선 무시 가능,
        # 병목이면 라인별 토큰 수 합산 + 개행 여유분으로 교체
        elif buf and _count_tokens("\n".join([*buf, line])) > max_tokens:
            pieces.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        pieces.append("\n".join(buf))
    return pieces


def _chunk_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """고정 크기 청킹 + 오버랩. 단락/문장 경계 존중."""
    if _count_tokens(text) <= max_tokens:
        return [text.strip()] if text.strip() else []

    # max_tokens 초과 단락이 단독 청크로 통과하면 리랭커 max_length(512)에서 뒷부분이 잘린다.
    paragraphs: list[str] = []
    for para in text.split("\n\n"):
        if _count_tokens(para) > max_tokens:
            paragraphs.extend(_split_oversized_paragraph(para, max_tokens))
        else:
            paragraphs.append(para)
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = _count_tokens(para)

        if current_tokens + para_tokens > max_tokens and current_parts:
            chunk_text = "\n\n".join(current_parts).strip()
            if chunk_text:
                chunks.append(chunk_text)

            # 오버랩: 마지막 부분을 유지
            overlap_parts: list[str] = []
            overlap_count = 0
            for part in reversed(current_parts):
                part_tokens = _count_tokens(part)
                if overlap_count + part_tokens > overlap_tokens:
                    break
                overlap_parts.insert(0, part)
                overlap_count += part_tokens
            current_parts = overlap_parts
            current_tokens = overlap_count

        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        chunk_text = "\n\n".join(current_parts).strip()
        if chunk_text:
            chunks.append(chunk_text)

    return chunks


# 헤딩·링크·태그 전용 초소형 청크가 리랭크 top을 점령하는 것을 막기 위한 병합 임계값.
MIN_CHUNK_CHARS = 80


def _merge_tiny_chunks(chunks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """MIN_CHUNK_CHARS 미만인 저정보 청크를 인접 청크에 병합. 결정적 순수 함수.

    - tiny + 다음 청크가 있으면 → 다음 청크 앞에 병합 (헤딩이 후속 본문의 문맥이 되도록
      section_path는 다음 청크 것 유지)
    - tiny가 마지막 청크면 → 이전 청크 뒤에 병합 (section_path는 이전 것 유지)
    - 문서 전체가 tiny 1개뿐이면 그대로 유지
    - 왼쪽부터 반복 처리해 연쇄 tiny(연속 2개 이상)도 자연스럽게 병합
    """
    merged = list(chunks)
    i = 0
    while i < len(merged):
        _, content = merged[i]
        if len(merged) == 1 or len(content) >= MIN_CHUNK_CHARS:
            i += 1
            continue
        if i < len(merged) - 1:
            next_path, next_content = merged[i + 1]
            merged[i + 1] = (next_path, content + "\n\n" + next_content)
            del merged[i]
        else:
            prev_path, prev_content = merged[i - 1]
            merged[i - 1] = (prev_path, prev_content + "\n\n" + content)
            del merged[i]
            i -= 1
    return merged


def chunk_markdown_hierarchical(text: str) -> list[tuple[str, str]]:
    """섹션 경로를 유지하며 청크 생성. [(section_path, chunk_text), ...]"""
    sections = _split_by_headings_hierarchical(text)
    all_chunks: list[tuple[str, str]] = []

    for section_path, section in sections:
        chunks = _chunk_text(section, settings.chunk_size, settings.chunk_overlap)
        for chunk in chunks:
            all_chunks.append((section_path, chunk))

    return _merge_tiny_chunks(all_chunks)


def _detect_language(text: str) -> str:
    """간단한 한국어/영어 감지."""
    korean_chars = sum(1 for c in text[:500] if "\uac00" <= c <= "\ud7a3")
    return "ko" if korean_chars > 10 else "en"


def _nfc(text: str) -> str:
    """macOS 파일시스템이 돌려주는 NFD 경로 문자열을 NFC로 통일.

    doc_id·category가 NFC/NFD 두 벌로 갈라져 색인이 중복되는 것을 막는다
    — 경로에서 파생되는 모든 문자열은 저장 전에 반드시 여길 거친다.
    """
    return unicodedata.normalize("NFC", text)


def _extract_category(file_path: Path, base_dir: Path) -> str:
    """파일 경로에서 카테고리 추출 — 코퍼스 최상위 폴더명이 곧 카테고리 (동적).

    폴더 추가가 곧 카테고리 추가. 루트 직속 파일과 숨김 폴더(도구 산출물)는 misc.
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        return "misc"
    if len(rel.parts) < 2 or rel.parts[0].startswith("."):
        return "misc"
    return _nfc(rel.parts[0])


_NUM_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*[_\s\-\.]*")


def _clean_path_name(name: str) -> str:
    """경로 조각에서 번호 prefix/언더스코어를 정리해 사람이 읽기 쉬운 형태로.
    예: '4.2.6_청크_크기_선택' → '청크 크기 선택'
    """
    name = _NUM_PREFIX_RE.sub("", name)
    name = name.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", name).strip()


def derive_section_path_from_path(file_path: Path, base_dir: Path) -> str:
    """파일 경로에서 section_path를 파생 (H1~H3 헤딩이 없는 문서의 fallback).

    첫 두 조각(카테고리 + 서브루트)은 제외하고, 그 아래 계층만 사람이 읽기 쉬운
    형태로 조합한다. 예:
      data/study/rag-v2/4_신뢰·운영·측정/4.2_코퍼스·청킹·인덱스/4.2.6_청크_크기_선택.md
      → "신뢰·운영·측정 > 코퍼스·청킹·인덱스 > 청크 크기 선택"
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        return ""

    parts = list(rel.parts)
    if parts and parts[0] == "data":
        parts = parts[1:]
    if not parts:
        return ""
    # 파일명에서 확장자 제거
    parts[-1] = Path(parts[-1]).stem

    cleaned = [c for c in (_clean_path_name(p) for p in parts) if c]
    if len(cleaned) > 2:
        # 첫 두 단계(카테고리 + 서브루트, 예: study > rag-v2)는 section으로서 가치 낮음
        cleaned = cleaned[2:]
    return _nfc(" > ".join(cleaned))


def _extract_title(text: str, file_path: Path) -> str:
    """마크다운에서 제목 추출. 없으면 파일명에서 번호/언더스코어 정리 후 사용."""
    for line in text.split("\n")[:10]:
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    cleaned = _clean_path_name(file_path.stem)
    return _nfc(cleaned or file_path.stem)


def process_file(
    file_path: Path,
    base_dir: Path,
    doc_id_prefix: str = "",
    category_override: str | None = None,
) -> list[dict]:
    """파일을 읽고 청크 + 메타데이터 리스트 반환.
    md/txt는 그대로, pdf는 페이지 보존 추출, docx/pptx/xlsx/html은 markitdown으로 변환.

    Args:
        file_path: 처리할 파일의 절대경로
        base_dir: 상대경로 계산 기준 디렉터리
        doc_id_prefix: doc_id 앞에 붙일 접두사 (예: "obsidian/"). 외부 경로 인제스트 시 사용.
        category_override: None이 아니면 경로 기반 카테고리 대신 이 값 사용.
    """
    if is_excluded_path(file_path):
        # 개념노트·예약 디렉터리·숨김 경로는 색인 금지. 탐색(find_ingestable_files)뿐 아니라
        # 여기서도 막는다 — write_file/add_document는 파일을 직접 ingest_files에 넘긴다.
        return []
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return []
    raw_text = read_file_as_text(file_path)
    if not raw_text.strip():
        return []

    # YAML frontmatter 추출 (md/markdown 파일만)
    frontmatter: dict = {}
    text = raw_text
    if file_path.suffix.lower() in {".md", ".markdown"}:
        frontmatter, text = parse_frontmatter(raw_text)

    chunks_with_path = chunk_markdown_hierarchical(text)
    if not chunks_with_path:
        return []

    # H1~H3 헤딩이 전혀 없어 section_path가 모두 빈 경우, 파일 경로에서 파생된 값으로 대체.
    if all(not sp for sp, _ in chunks_with_path):
        derived = derive_section_path_from_path(file_path, base_dir)
        if derived:
            chunks_with_path = [(derived, c) for _, c in chunks_with_path]

    rel = _nfc(str(file_path.relative_to(base_dir)))
    doc_id = f"{doc_id_prefix}{rel}" if doc_id_prefix else rel
    category = category_override or _extract_category(file_path, base_dir)

    # frontmatter에서 메타데이터 추출 (있으면 우선)
    fm_title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else None
    fm_tags = frontmatter.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [t.strip() for t in fm_tags.split(",") if t.strip()]
    elif not isinstance(fm_tags, list):
        fm_tags = []

    title = fm_title or _extract_title(text, file_path)
    language = _detect_language(text)
    mtime = datetime.fromtimestamp(
        os.path.getmtime(file_path), tz=UTC
    ).strftime("%Y-%m-%d")
    fm_expires_at = parse_expires_at(frontmatter.get("expires_at"))
    # 아카이브 상태도 frontmatter가 SSOT — reindex/재인제스트 시 유실되지 않게 청크에 반영.
    fm_archived_at = parse_expires_at(frontmatter.get("archived_at"))
    fm_archive_reason = frontmatter.get("archive_reason")
    if not isinstance(fm_archive_reason, str):
        fm_archive_reason = None
    document_metadata = _normalize_document_metadata(frontmatter)

    results = []
    for i, (section_path, chunk_text) in enumerate(chunks_with_path):
        chunk: dict = {
            "content": chunk_text,
            "content_hash": _content_hash(chunk_text),
            "source_path": doc_id,
            "category": category,
            "doc_id": doc_id,
            "chunk_index": i,
            "section_path": section_path,
            "title": title,
            "tags": [str(t) for t in fm_tags],
            "date_modified": mtime,
            "language": language,
        }
        # Keep optional contract metadata absent for legacy notes.  Copy list
        # values per chunk so a caller mutating one result cannot accidentally
        # alter the metadata attached to its siblings.
        for key, value in document_metadata.items():
            chunk[key] = list(value) if isinstance(value, list) else value
        if fm_expires_at:
            chunk["expires_at"] = fm_expires_at
        if fm_archived_at:
            chunk["archived_at"] = fm_archived_at
        if fm_archive_reason:
            chunk["archive_reason"] = fm_archive_reason
        chunk["embedding_fingerprint"] = embedding_fingerprint(chunk)
        results.append(chunk)
    return results


def _empty_stats() -> dict:
    return {
        "files": 0,
        "reused": 0,
        "moved": 0,
        "embedded": 0,
        "added": 0,
        "metadata_updated": 0,
        "deleted": 0,
    }


@dataclass
class _PreparedDelta:
    doc_id: str
    new_by_idx: dict[int, dict]
    existing: dict[int, dict]
    to_embed_indices: list[int]
    moved_indices: list[int]
    metadata_updates: list[tuple[int, dict]]
    delete_indices: list[int]
    reused: int


def ingest_files(
    file_paths: list[Path],
    base_dir: Path,
    doc_id_prefix: str = "",
    category_override: str | None = None,
    tag_override: list[str] | None = None,
) -> dict:
    """파일 리스트를 델타 인제스트.

    각 청크 슬롯을 chunk_index 단위로 비교 (`_id = f"{doc_id}_{chunk_index}"`):
      - hash 일치 → 그대로 (메타데이터 차이만 partial update, 임베딩 유지)
      - 슬롯 불일치지만 같은 content_hash가 문서 내 다른 슬롯에 존재
        → 기존 임베딩 복사 재색인 (moved, 재임베딩 없음)
        — 중간 삽입으로 chunk_index가 시프트돼도 변경분만 임베딩
      - 그 외 신규/변경 → 재임베딩 + index
      - 새 청크에서 사라진 슬롯 → delete
    """
    from pkb.embeddings import embed
    from pkb.search_log import log_change
    from pkb.store import (
        apply_chunk_delta,
        get_chunk_embeddings,
        get_client,
        get_existing_chunks,
    )

    es = get_client()
    stats = _empty_stats()
    relayouts: list[tuple[str, dict[int, str], dict[int, str]]] = []
    prepared: list[_PreparedDelta] = []
    embedding_targets: list[tuple[_PreparedDelta, int]] = []
    embedding_texts: list[str] = []

    for file_path in file_paths:
        new_chunks = process_file(
            file_path, base_dir,
            doc_id_prefix=doc_id_prefix,
            category_override=category_override,
        )
        if not new_chunks:
            continue
        if tag_override is not None:
            for c in new_chunks:
                c["tags"] = tag_override

        doc_id = new_chunks[0]["doc_id"]
        existing = get_existing_chunks(es, doc_id)
        new_by_idx = {c["chunk_index"]: c for c in new_chunks}

        # 벡터 재사용·복사 판정은 embedding_fingerprint(모델+전처리+임베딩 입력) 기준.
        # fingerprint가 없는 구 청크는 어떤 구성으로 임베딩됐는지 알 수 없으므로 재임베딩
        # — 마이그레이션 순서 함정(모델 교체 후 구벡터에 새 fingerprint 백필) 방지.
        old_idx_by_fp: dict[str, int] = {}
        for idx, old in existing.items():
            fp = old.get("embedding_fingerprint")
            if fp is not None and fp not in old_idx_by_fp:
                old_idx_by_fp[fp] = idx

        to_embed_indices: list[int] = []
        to_copy: list[tuple[int, int]] = []  # (new_idx, old_idx) 슬롯 이동
        metadata_updates: list[tuple[int, dict]] = []
        delete_indices: list[int] = []
        reused = 0

        for idx, new in new_by_idx.items():
            old = existing.get(idx)
            if old is None or old.get("embedding_fingerprint") != new["embedding_fingerprint"]:
                # 슬롯 불일치 — 같은 fingerprint가 다른 슬롯에 있으면 임베딩 복사
                src_idx = old_idx_by_fp.get(new["embedding_fingerprint"])
                if src_idx is not None:
                    to_copy.append((idx, src_idx))
                else:
                    to_embed_indices.append(idx)
                continue
            meta_diff = _diff_metadata(old, new)
            if meta_diff:
                metadata_updates.append((idx, meta_diff))
            else:
                reused += 1

        for idx in existing:
            if idx not in new_by_idx:
                delete_indices.append(idx)

        moved_indices: list[int] = []
        if to_copy:
            src_vecs = get_chunk_embeddings(
                es, doc_id, sorted({src for _, src in to_copy})
            )
            for idx, src_idx in to_copy:
                vec = src_vecs.get(src_idx)
                if vec is None:
                    # 방어: 구 청크에 임베딩이 없으면 재임베딩
                    to_embed_indices.append(idx)
                else:
                    new_by_idx[idx]["embedding"] = vec
                    moved_indices.append(idx)

        delta = _PreparedDelta(
            doc_id=doc_id,
            new_by_idx=new_by_idx,
            existing=existing,
            to_embed_indices=to_embed_indices,
            moved_indices=moved_indices,
            metadata_updates=metadata_updates,
            delete_indices=delete_indices,
            reused=reused,
        )
        prepared.append(delta)
        for idx in to_embed_indices:
            embedding_targets.append((delta, idx))
            embedding_texts.append(embed_input(new_by_idx[idx]))

    # 파일별 encode 호출 대신 이번 ingest_files 호출의 신규/변경 청크를 한 번에 배치한다.
    if embedding_texts:
        vectors = embed(embedding_texts)
        for (delta, idx), vector in zip(embedding_targets, vectors, strict=True):
            delta.new_by_idx[idx]["embedding"] = vector

    for prepared_delta in prepared:
        doc_id = prepared_delta.doc_id
        new_by_idx = prepared_delta.new_by_idx
        existing = prepared_delta.existing
        to_embed_indices = prepared_delta.to_embed_indices
        moved_indices = prepared_delta.moved_indices
        metadata_updates = prepared_delta.metadata_updates
        delete_indices = prepared_delta.delete_indices
        indexed_indices = to_embed_indices + moved_indices
        added = sum(1 for i in indexed_indices if existing.get(i) is None)
        re_embedded = sum(1 for i in to_embed_indices if existing.get(i) is not None)

        apply_chunk_delta(
            es,
            doc_id,
            new_chunks=[new_by_idx[i] for i in indexed_indices],
            metadata_updates=metadata_updates,
            delete_indices=delete_indices,
        )

        old_hashes = {
            i: o["content_hash"] for i, o in existing.items() if o.get("content_hash")
        }
        new_hashes = {i: c["content_hash"] for i, c in new_by_idx.items()}
        if old_hashes and old_hashes != new_hashes:
            # 청크 레이아웃이 바뀐 문서 — 개념 멘션도 새 슬롯을 따라가야 한다 (루프 뒤 일괄 처리)
            relayouts.append((doc_id, old_hashes, new_hashes))

        delta = {
            "new": added,
            "updated": re_embedded + len(metadata_updates),
            "moved": len(moved_indices),
            "deleted": len(delete_indices),
        }
        if any(delta.values()):
            log_change("ingest", doc_id, chunks=delta)

        _log.info(
            "[delta] %s reused=%d moved=%d re-embedded=%d added=%d "
            "metadata_updated=%d deleted=%d",
            doc_id,
            prepared_delta.reused,
            len(moved_indices),
            re_embedded,
            added,
            len(metadata_updates),
            len(delete_indices),
        )
        stats["files"] += 1
        stats["reused"] += prepared_delta.reused
        stats["moved"] += len(moved_indices)
        stats["embedded"] += re_embedded
        stats["added"] += added
        stats["metadata_updated"] += len(metadata_updates)
        stats["deleted"] += len(delete_indices)

    if relayouts:
        _realign_graph(relayouts)
    return stats


def _realign_graph(relayouts: list[tuple[str, dict[int, str], dict[int, str]]]) -> None:
    """청크 레이아웃이 바뀐 문서들의 개념 멘션 위치를 새 슬롯에 맞춘다.

    그래프 DB는 선택 기능이라 실패해도 인제스트를 막지 않는다 (색인은 이미 끝난 상태).
    """
    from pkb.graph.schema import graph_connection
    from pkb.graph.store import realign_doc_chunks

    try:
        with graph_connection(settings.graph_db_path) as conn:
            moved = dropped = 0
            for doc_id, old_hashes, new_hashes in relayouts:
                r = realign_doc_chunks(conn, doc_id, old_hashes, new_hashes)
                moved += r["moved"]
                dropped += r["dropped"]
        if moved or dropped:
            _log.info(
                "[graph] 멘션 재정렬: %d개 문서, 이동=%d 삭제=%d",
                len(relayouts), moved, dropped,
            )
    except Exception:
        _log.warning("[graph] 멘션 재정렬 실패 — 그래프 위치가 낡았을 수 있다", exc_info=True)


# 검토 큐 디렉터리. 승인 전 대기 노트가 전량 색인에 끌려가지 않도록 탐색에서 제외한다.
# _materials: 강의 PDF 원본 — 같은 폴더 _extracted md가 전량(72/72) 존재해 이중 색인만
# 유발 (2026-07-10 실측).
# _origin: 외부 원본 보관소 — 소화 노트만 색인하고 원본은 근거 확인용으로만 둔다.
EXCLUDED_DIR_NAMES = {
    "_review",
    "_trash",
    "_materials",
    "_archive",
    "_origin",
    "_concepts",  # 제거된 레거시 투영 폴더가 복원돼도 중복 색인하지 않음
}


def _has_dot_segment(path: Path) -> bool:
    """경로 성분 중 '.'으로 시작하는 세그먼트가 있는지 (.obsidian 등 도구 산출물 차단)."""
    return any(part.startswith(".") for part in path.parts)


def is_excluded_path(path: Path) -> bool:
    """색인 제외 경로인지 — 예약 디렉터리/숨김 성분.

    탐색(find_ingestable_files)과 직접 인제스트(process_file) 양쪽이 같은 판정을 쓴다.
    """
    return _has_dot_segment(path) or not EXCLUDED_DIR_NAMES.isdisjoint(path.parts)


def find_ingestable_files(path: Path, exclude: Path | None = None) -> list[Path]:
    """경로에서 인제스트 가능한 파일 찾기. md/txt/pdf/docx/pptx/xlsx/html 지원.

    경로에 _review/_trash/_materials/_archive/_origin 성분이 있으면 검토 큐·중복·보관·외부
    원본으로 보고 제외한다.
    "."으로 시작하는 경로 성분(.obsidian 등 도구 산출물)도 제외한다.
    exclude가 주어지면 그 서브트리는 건너뛴다 (예: 볼트 크롤 시 data 코퍼스 중복 방지).
    """
    if path.is_file():
        if exclude is not None and path.is_relative_to(exclude):
            return []
        if is_excluded_path(path):
            return []
        return [path] if path.suffix.lower() in SUPPORTED_EXTENSIONS else []
    if path.is_dir():
        files = [
            p for p in path.rglob("*")
            if p.is_file()
            and p.suffix.lower() in SUPPORTED_EXTENSIONS
            and not is_excluded_path(p)
            and (exclude is None or not p.is_relative_to(exclude))
        ]
        return sorted(files)
    return []


# 하위 호환용 별칭
find_markdown_files = find_ingestable_files


def write_sync_marker(ts: str | None = None) -> None:
    """sync 마커 기록 — pkb stale이 읽는다. .logs는 dot-segment 폴더라 인제스트 제외가 자동 보장됨.

    ponytail: 단일 타임스탬프 — 트리 하나만 재조정해도 전체가 fresh로 보인다.
    stale 넛지 용도라 허용; 정밀해지려면 prefix별 마커로 분리.
    """
    from pkb.search_log import LAST_SYNC_FILE

    LAST_SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SYNC_FILE.write_text(
        json.dumps({"ts": ts or datetime.now(UTC).isoformat()}), encoding="utf-8"
    )


def reconcile(
    es,
    root: Path,
    prefix: str,
    category_override: str | None = None,
    exclude: Path | None = None,
) -> tuple[dict, list[str]]:
    """root를 ES와 재조정. (stats, stale_doc_ids) 반환.

    prune(삭제)/confirm 판단은 호출자 책임 — 여기서는 절대 삭제하지 않는다.
    """
    from pkb.store import list_doc_ids

    # 마커 타임스탬프는 파일 스캔 시작 직전 캡처 — 완료 시점(now)으로 기록하면
    # sync 도중 수정된 파일이 마커보다 과거 mtime이 되어 fresh로 오탐된다.
    scan_ts = datetime.now(UTC).isoformat()
    files = find_ingestable_files(root, exclude=exclude)
    stats = ingest_files(files, base_dir=root, doc_id_prefix=prefix, category_override=category_override)
    expected = {f"{prefix}{_nfc(str(f.relative_to(root)))}" for f in files}
    stale = sorted(list_doc_ids(es, prefix) - expected)

    write_sync_marker(scan_ts)
    return stats, stale
