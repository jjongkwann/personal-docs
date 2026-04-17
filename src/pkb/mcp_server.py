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
    """파일을 작성합니다. 블로그 포스트, 이력서, 메모 등을 마크다운 파일로 저장합니다.
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
    """마크다운 파일을 지식 베이스에 인제스트합니다.

    Args:
        file_path: 인제스트할 마크다운 파일 경로 (예: data/career/resume.md)
        tags: 쉼표 구분 태그 (예: python,backend)
    """
    from pathlib import Path

    from pkb.embeddings import embed
    from pkb.ingest import process_file
    from pkb.store import add_chunks, delete_document, get_client

    base_dir = Path.cwd()
    data_root = (base_dir / "data").resolve()
    full_path = (base_dir / file_path).resolve()

    if not full_path.is_relative_to(data_root):
        return f"오류: data/ 하위 경로만 인제스트할 수 있습니다. (입력: {file_path})"
    if not full_path.exists():
        return f"파일을 찾을 수 없습니다: {file_path}"
    if full_path.suffix != ".md":
        return f"마크다운 파일만 지원합니다: {file_path}"

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


if __name__ == "__main__":
    mcp.run()
