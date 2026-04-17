import os
from datetime import datetime, timezone
from pathlib import Path

import tiktoken

from pkb.config import settings

_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def _count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def _split_by_headings(text: str) -> list[str]:
    """마크다운 ## 헤딩 기준으로 분할."""
    sections = []
    current: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## ") and current:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        sections.append("\n".join(current))
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
    """마크다운 텍스트를 청크로 분할. 헤딩 경계 우선, 고정 크기 폴백."""
    sections = _split_by_headings(text)
    all_chunks: list[str] = []

    for section in sections:
        chunks = _chunk_text(section, settings.chunk_size, settings.chunk_overlap)
        all_chunks.extend(chunks)

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


def process_file(file_path: Path, base_dir: Path) -> list[dict]:
    """파일을 읽고 청크 + 메타데이터 리스트 반환."""
    text = file_path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    chunks = chunk_markdown(text)
    if not chunks:
        return []

    doc_id = str(file_path.relative_to(base_dir))
    category = _extract_category(file_path, base_dir)
    title = _extract_title(text, file_path)
    language = _detect_language(text)
    mtime = datetime.fromtimestamp(
        os.path.getmtime(file_path), tz=timezone.utc
    ).strftime("%Y-%m-%d")

    results = []
    for i, chunk in enumerate(chunks):
        results.append(
            {
                "content": chunk,
                "source_path": str(file_path.relative_to(base_dir)),
                "category": category,
                "doc_id": doc_id,
                "chunk_index": i,
                "title": title,
                "tags": [],
                "date_modified": mtime,
                "language": language,
            }
        )
    return results


def find_markdown_files(path: Path) -> list[Path]:
    """경로에서 마크다운 파일 찾기. 파일이면 그대로, 디렉터리면 재귀 탐색."""
    if path.is_file() and path.suffix == ".md":
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.md"))
    return []
