from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from pkb.config import settings
from pkb.retrieve import hybrid_search
from pkb.store import get_client, list_documents as _list_documents


@tool
def search_knowledge(
    query: str,
    category: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """개인 지식 베이스에서 관련 정보를 검색합니다.
    사용자의 질문에 답하기 위해 저장된 문서에서 관련 내용을 찾습니다.

    Args:
        query: 검색할 질문 또는 키워드
        category: 카테고리 필터 (about: 자기소개, career: 경력, study: 공부 노트, writing: 글쓰기). None이면 전체 검색.
        top_k: 반환할 결과 수
    """
    es = get_client()
    results = hybrid_search(
        es, query,
        category=category, top_k=top_k,
        candidate_k=settings.candidate_k,
        fusion=settings.fusion,
        rerank=settings.rerank_enabled,
        expand_context=settings.expand_context,
    )

    if not results:
        return "검색 결과가 없습니다."

    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[출처 {i}: {r['source_path']} | 카테고리: {r['category']}]\n{r['content']}\n"
        )
    return "\n".join(parts)


@tool
def write_file(file_path: str, content: str, ingest: bool = True) -> str:
    """파일을 작성합니다. 문서, 요약, 메모 등을 마크다운 파일로 저장합니다.

    Args:
        file_path: 저장할 파일 경로 (예: data/writing/blog-post.md). data/ 하위 경로만 허용.
        content: 파일에 작성할 내용
        ingest: True면 저장 후 ES에 자동 인제스트 (기본값 True)
    """
    data_root = (Path.cwd() / "data").resolve()
    full_path = (Path.cwd() / file_path).resolve()

    # data/ 디렉터리 포함 검사 (.. 순회 차단)
    if not full_path.is_relative_to(data_root):
        return f"오류: data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
    if full_path.suffix != ".md":
        return f"오류: 마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})"

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")

    result = f"파일 저장 완료: {file_path} ({len(content)}자)"
    if ingest:
        from pkb.ingest import ingest_files

        count = ingest_files([full_path], base_dir=Path.cwd())
        result += f" | 인제스트: {count}개 청크"
    return result


@tool
def list_documents(category: Optional[str] = None) -> str:
    """저장된 문서 목록을 확인합니다.

    Args:
        category: 카테고리 필터 (about, career, study, writing). None이면 전체.
    """
    es = get_client()
    docs = _list_documents(es, category)

    if not docs:
        return "저장된 문서가 없습니다."

    lines = [f"총 {len(docs)}개 문서:\n"]
    for doc in docs:
        lines.append(
            f"- {doc['doc_id']} [{doc['category']}] ({doc['chunks']}개 청크)"
        )
    return "\n".join(lines)
