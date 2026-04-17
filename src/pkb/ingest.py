import os
import re
from datetime import datetime, timezone
from pathlib import Path

import tiktoken
import yaml

from pkb.config import settings

# markitdown이 마크다운으로 변환 가능한 포맷 + 원본 마크다운/텍스트
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


def read_file_as_text(file_path: Path) -> str:
    """파일을 텍스트로 변환. md/txt는 그대로, 나머지는 markitdown 사용."""
    ext = file_path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return file_path.read_text(encoding="utf-8")
    result = _get_markitdown().convert(str(file_path))
    return result.text_content


VALID_CATEGORIES = {"about", "career", "study", "writing", "misc", "obsidian"}


def classify_category(text: str) -> str:
    """Claude로 문서 내용 기반 카테고리 자동 분류."""
    from langchain_anthropic import ChatAnthropic

    preview = text[:2000]
    llm = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        api_key=settings.anthropic_api_key,
        max_tokens=10,
    )
    prompt = f"""다음 문서를 카테고리 하나로 분류하세요. 답변은 카테고리 이름 한 단어만.

카테고리:
- about: 자기소개, 개인 관심사/취향
- career: 경력, 프로젝트, 기술 스택, 업무 이력
- study: 공부 노트, 학습 자료, 교재, 논문
- writing: 글 초안, 아이디어, 에세이
- misc: 위 중 어느 것도 아닌 경우

문서 내용 (앞부분):
{preview}

카테고리:"""
    response = llm.invoke(prompt)
    content = response.content if isinstance(response.content, str) else str(response.content)
    category = content.strip().lower()
    return category if category in VALID_CATEGORIES else "misc"


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


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

    for line in text.split("\n"):
        m = re.match(r"^(#{1,3})\s+(.+)$", line)
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


def _chunk_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """고정 크기 청킹 + 오버랩. 단락/문장 경계 존중."""
    if _count_tokens(text) <= max_tokens:
        return [text.strip()] if text.strip() else []

    paragraphs = text.split("\n\n")
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


def chunk_markdown(text: str) -> list[str]:
    """하위 호환용: section_path를 버리고 텍스트 청크 리스트만 반환."""
    return [c[1] for c in chunk_markdown_hierarchical(text)]


def chunk_markdown_hierarchical(text: str) -> list[tuple[str, str]]:
    """섹션 경로를 유지하며 청크 생성. [(section_path, chunk_text), ...]"""
    sections = _split_by_headings_hierarchical(text)
    all_chunks: list[tuple[str, str]] = []

    for section_path, section in sections:
        chunks = _chunk_text(section, settings.chunk_size, settings.chunk_overlap)
        for chunk in chunks:
            all_chunks.append((section_path, chunk))

    return all_chunks


def _detect_language(text: str) -> str:
    """간단한 한국어/영어 감지."""
    korean_chars = sum(1 for c in text[:500] if "\uac00" <= c <= "\ud7a3")
    return "ko" if korean_chars > 10 else "en"


def _extract_category(file_path: Path, base_dir: Path) -> str:
    """파일 경로에서 카테고리 추출."""
    try:
        rel = file_path.relative_to(base_dir / "data")
        category = rel.parts[0] if rel.parts else "misc"
        if category in ("about", "career", "study", "writing"):
            return category
    except ValueError:
        pass
    return "misc"


def _extract_title(text: str, file_path: Path) -> str:
    """마크다운에서 제목 추출. 없으면 파일명 사용."""
    for line in text.split("\n")[:10]:
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return file_path.stem


def process_file(
    file_path: Path,
    base_dir: Path,
    doc_id_prefix: str = "",
    category_override: str | None = None,
) -> list[dict]:
    """파일을 읽고 청크 + 메타데이터 리스트 반환.
    md/txt는 그대로, pdf/docx/pptx/xlsx/html은 markitdown으로 변환.

    Args:
        file_path: 처리할 파일의 절대경로
        base_dir: 상대경로 계산 기준 디렉터리
        doc_id_prefix: doc_id 앞에 붙일 접두사 (예: "obsidian/"). 외부 경로 인제스트 시 사용.
        category_override: None이 아니면 경로 기반 카테고리 대신 이 값 사용.
    """
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

    rel = str(file_path.relative_to(base_dir))
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
        os.path.getmtime(file_path), tz=timezone.utc
    ).strftime("%Y-%m-%d")

    results = []
    for i, (section_path, chunk_text) in enumerate(chunks_with_path):
        results.append(
            {
                "content": chunk_text,
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
        )
    return results


def ingest_files(
    file_paths: list[Path],
    base_dir: Path,
    doc_id_prefix: str = "",
    category_override: str | None = None,
) -> int:
    """파일 리스트를 처리 → 임베딩 → ES에 저장. 처리된 청크 수 반환."""
    from pkb.embeddings import embed
    from pkb.store import add_chunks, delete_document, get_client

    es = get_client()
    total = 0
    for file_path in file_paths:
        chunks = process_file(
            file_path, base_dir,
            doc_id_prefix=doc_id_prefix,
            category_override=category_override,
        )
        if not chunks:
            continue
        delete_document(es, chunks[0]["doc_id"])
        texts = [c["content"] for c in chunks]
        vectors = embed(texts)
        for chunk, vector in zip(chunks, vectors):
            chunk["embedding"] = vector
        total += add_chunks(es, chunks)
    return total


def find_ingestable_files(path: Path) -> list[Path]:
    """경로에서 인제스트 가능한 파일 찾기. md/txt/pdf/docx/pptx/xlsx/html 지원."""
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_EXTENSIONS else []
    if path.is_dir():
        files = [
            p for p in path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        return sorted(files)
    return []


# 하위 호환용 별칭
find_markdown_files = find_ingestable_files
