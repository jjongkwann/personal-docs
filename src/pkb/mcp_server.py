"""PKB MCP Server — Claude Code에서 개인 지식 베이스에 직접 접근."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pkb", instructions="""개인 지식 관리 시스템(PKB)입니다.
사용자의 개인 데이터(경력, 공부 노트, 자기소개 등)가 Elasticsearch에 저장되어 있습니다.
질문에 답하려면 search_knowledge로 검색하고, 파일 작성은 write_file을 사용하세요.""")


@mcp.tool()
def search_knowledge(query: str, category: str = "", top_k: int = 5) -> str:
    """개인 지식 베이스에서 관련 정보를 하이브리드 검색(BM25+kNN)합니다.

    Args:
        query: 검색할 질문 또는 키워드
        category: 카테고리 필터 (about: 자기소개, career: 경력, study: 공부, writing: 글). 빈 문자열이면 전체 검색.
        top_k: 반환할 결과 수
    """
    from pkb.retrieve import hybrid_search
    from pkb.store import get_client

    es = get_client()
    results = hybrid_search(
        es, query, category=category or None, top_k=top_k
    )

    if not results:
        return "검색 결과가 없습니다."

    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[출처 {i}: {r['source_path']} | 카테고리: {r['category']}]\n{r['content']}\n"
        )
    return "\n".join(parts)


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """파일을 작성합니다. 문서, 요약, 메모 등을 마크다운 파일로 저장합니다.
    data/ 하위 경로에만 저장 가능합니다.

    Args:
        file_path: 저장할 파일 경로 (예: data/writing/blog-post.md)
        content: 파일에 작성할 내용
    """
    from pathlib import Path

    data_root = (Path.cwd() / "data").resolve()
    full_path = (Path.cwd() / file_path).resolve()

    if not full_path.is_relative_to(data_root):
        return f"오류: data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
    if full_path.suffix != ".md":
        return f"오류: 마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})"

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")

    return f"파일 저장 완료: {file_path} ({len(content)}자)"


@mcp.tool()
def list_documents(category: str = "") -> str:
    """저장된 문서 목록을 확인합니다.

    Args:
        category: 카테고리 필터 (about, career, study, writing). 빈 문자열이면 전체.
    """
    from pkb.store import get_client
    from pkb.store import list_documents as _list_documents

    es = get_client()
    docs = _list_documents(es, category or None)

    if not docs:
        return "저장된 문서가 없습니다."

    lines = [f"총 {len(docs)}개 문서:\n"]
    for doc in docs:
        lines.append(
            f"- {doc['doc_id']} [{doc['category']}] ({doc['chunks']}개 청크)"
        )
    return "\n".join(lines)


@mcp.tool()
def add_document(file_path: str, tags: str = "") -> str:
    """파일을 지식 베이스에 인제스트합니다. md, txt, pdf, docx, pptx, xlsx, html 지원.

    Args:
        file_path: 인제스트할 파일 경로 (예: data/study/paper.pdf)
        tags: 쉼표 구분 태그 (예: python,backend)
    """
    from pathlib import Path

    from pkb.embeddings import embed
    from pkb.ingest import SUPPORTED_EXTENSIONS, process_file
    from pkb.store import add_chunks, delete_document, get_client

    base_dir = Path.cwd()
    data_root = (base_dir / "data").resolve()
    full_path = (base_dir / file_path).resolve()

    if not full_path.is_relative_to(data_root):
        return f"오류: data/ 하위 경로만 인제스트할 수 있습니다. (입력: {file_path})"
    if not full_path.exists():
        return f"파일을 찾을 수 없습니다: {file_path}"
    if full_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return f"지원하지 않는 파일 형식입니다: {file_path} (지원: {sorted(SUPPORTED_EXTENSIONS)})"

    chunks = process_file(full_path, base_dir)
    if not chunks:
        return f"빈 파일입니다: {file_path}"

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if tag_list:
        for chunk in chunks:
            chunk["tags"] = tag_list

    es = get_client()
    doc_id = chunks[0]["doc_id"]
    delete_document(es, doc_id)

    texts = [c["content"] for c in chunks]
    vectors = embed(texts)
    for chunk, vector in zip(chunks, vectors):
        chunk["embedding"] = vector

    count = add_chunks(es, chunks)
    return f"인제스트 완료: {file_path} ({count}개 청크)"


@mcp.tool()
def convert_and_ingest(
    input_path: str,
    category: str,
    output_name: str = "",
    ingest: bool = True,
) -> str:
    """PDF/DOCX/PPTX/XLSX/HTML 파일을 마크다운으로 변환하여 data/에 저장하고 인제스트합니다.
    원본 파일은 어느 위치에 있어도 되며, 변환된 .md는 data/<category>/에 저장됩니다.

    **카테고리 선택 가이드** (내용을 보고 직접 판단하여 지정):
    - about: 자기소개, 개인 관심사/취향
    - career: 경력, 프로젝트, 기술 스택, 업무 이력
    - study: 공부 노트, 학습 자료, 교재, 논문
    - writing: 글 초안, 아이디어, 에세이
    - misc: 위 중 어느 것도 아닌 경우

    파일 내용(또는 파일명/경로)을 보고 위 5가지 중 하나로 분류한 뒤 호출하세요.

    Args:
        input_path: 변환할 원본 파일 경로 (절대경로 가능)
        category: 저장할 카테고리. 위 5가지 중 하나를 내용 기반으로 선택.
        output_name: 저장할 파일명 (확장자 제외). 빈 문자열이면 원본 파일명 사용.
        ingest: 변환 후 자동 인제스트 여부
    """
    from pathlib import Path

    from pkb.embeddings import embed
    from pkb.ingest import (
        SUPPORTED_EXTENSIONS,
        VALID_CATEGORIES,
        process_file,
        read_file_as_text,
    )
    from pkb.store import add_chunks, delete_document, get_client

    src = Path(input_path).expanduser().resolve()
    if not src.exists():
        return f"파일을 찾을 수 없습니다: {input_path}"
    if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return f"지원하지 않는 형식입니다: {src.suffix} (지원: {sorted(SUPPORTED_EXTENSIONS)})"
    if category not in VALID_CATEGORIES:
        return f"유효하지 않은 카테고리: {category} (허용: {sorted(VALID_CATEGORIES)})"

    text = read_file_as_text(src)

    base_dir = Path.cwd()
    data_root = (base_dir / "data").resolve()
    stem = output_name or src.stem
    output = (data_root / category / f"{stem}.md").resolve()

    if not output.is_relative_to(data_root):
        return f"오류: 저장 경로가 data/ 밖입니다. (카테고리/파일명 확인: {category}/{stem})"

    output.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- source: {src.name} | converted: {src.suffix} → .md -->\n\n"
    output.write_text(header + text, encoding="utf-8")

    result = f"변환 완료: {output.relative_to(base_dir)} ({len(text)}자)"

    if ingest:
        chunks = process_file(output, base_dir)
        if chunks:
            es = get_client()
            delete_document(es, chunks[0]["doc_id"])
            texts = [c["content"] for c in chunks]
            vectors = embed(texts)
            for chunk, vector in zip(chunks, vectors):
                chunk["embedding"] = vector
            count = add_chunks(es, chunks)
            result += f"\n인제스트 완료: {count}개 청크"

    return result


@mcp.tool()
def sync_obsidian(path: str = "") -> str:
    """Obsidian 볼트를 지식 베이스에 동기화(일괄 인제스트)합니다.
    기존 obsidian/* 문서는 각 파일의 doc_id 기준으로 덮어쓰기됩니다.

    실시간 감시가 필요하면 별도 터미널에서 `uv run pkb watch`를 실행하세요.

    Args:
        path: Obsidian 볼트 절대경로. 빈 문자열이면 .env의 OBSIDIAN_PATH 사용.
    """
    from pathlib import Path

    from pkb.config import settings
    from pkb.ingest import find_ingestable_files, ingest_files

    vault_path = path or settings.obsidian_path
    if not vault_path:
        return "오류: 경로가 제공되지 않았고 .env에 OBSIDIAN_PATH도 없습니다."

    vault = Path(vault_path).expanduser().resolve()
    if not vault.is_dir():
        return f"디렉터리를 찾을 수 없습니다: {vault}"

    files = find_ingestable_files(vault)
    if not files:
        return f"인제스트할 파일이 없습니다: {vault}"

    total = ingest_files(
        files,
        base_dir=vault,
        doc_id_prefix="obsidian/",
        category_override="obsidian",
    )
    return f"Obsidian 동기화 완료: {len(files)}개 파일, {total}개 청크\n경로: {vault}"


if __name__ == "__main__":
    mcp.run()
