"""PKB MCP Server — Claude Code에서 개인 지식 베이스에 직접 접근."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pkb", instructions="""개인 지식 관리 시스템(PKB)의 기본 인터페이스입니다.
사용자의 개인 데이터(경력, 공부 노트, 자기소개, Obsidian 등)가 Elasticsearch에 저장되어 있습니다.
질문에 답하려면 search_knowledge로 검색하고, 파일 작성은 write_file을 사용하세요.
개념 관계는 볼트의 PKB/concepts/ 개념노트(md)를 직접 읽으세요 — 산문·관계 링크·출처가 담겨 있습니다.""")


def _resolve_data_path(file_path: str):
    """'data/...' doc_id 형식 경로(또는 코퍼스 하위 절대경로)를 실제 파일 경로로 변환.

    코퍼스 루트(DATA_ROOT)가 어디에 있든 도구 입력은 'data/<카테고리>/...' 형식을 유지한다.
    루트 밖으로 나가는 경로(절대경로·`..` 포함)면 None.
    """
    from pathlib import Path

    from pkb.config import data_dir

    root = data_dir()
    p = Path(file_path)
    if p.is_absolute():
        full = p.resolve()
    elif file_path == "data" or file_path.startswith("data/"):
        full = (root / file_path.removeprefix("data/").lstrip("/")).resolve()
    else:
        return None
    return full if full.is_relative_to(root) else None


def _graph_prune_summary(es) -> str:
    """그래프 DB가 있으면 ES 전체 doc_id 기준으로 dangling concept mention을 정리한다.

    코퍼스(data/obsidian) 재조정 후 sync 커맨드/도구 끝에서 호출. 그래프 DB 파일이
    없거나 정리할 게 없으면 빈 문자열.
    """
    from pathlib import Path

    from pkb.config import settings
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection, init_schema
    from pkb.store import list_doc_ids

    if not Path(settings.graph_db_path).exists():
        return ""
    existing = list_doc_ids(es, "data/") | list_doc_ids(es, "obsidian/")
    init_schema(settings.graph_db_path)
    with get_connection(settings.graph_db_path) as conn:
        result = gstore.prune_missing_documents(conn, existing)
    if not result["mentions_pruned"] and not result["documents_pruned"]:
        return ""
    return f"그래프 정리: mentions {result['mentions_pruned']}·documents {result['documents_pruned']}"


@mcp.tool()
def search_knowledge(
    query: str,
    category: str = "",
    top_k: int = 5,
    include_archived: bool = False,
    include_obsidian: bool = True,
) -> str:
    """개인 지식 베이스에서 관련 정보를 하이브리드 검색(BM25+kNN)합니다.
    RRF 결합 + CrossEncoder 재순위로 정밀도를 높입니다.

    Args:
        query: 검색할 질문 또는 키워드
        category: 카테고리 필터 = 코퍼스 최상위 폴더명 (about/career/study/writing/obsidian 등,
            전체 목록은 list_documents로 확인). 빈 문자열이면 전체.
        top_k: 반환할 결과 수
        include_archived: True면 아카이브/만료된 문서도 포함. 기본 False.
        include_obsidian: 볼트 일반 노트(전체 인덱스의 ~52%)를 제외하고 선별 코퍼스만
            검색하려면 False. 기본 True(포함).
    """
    from pkb.config import settings as _settings
    from pkb.retrieve import hybrid_search
    from pkb.store import get_client

    es = get_client()
    results = hybrid_search(
        es, query,
        category=category or None, top_k=top_k,
        candidate_k=_settings.candidate_k,
        rerank=_settings.rerank_enabled,
        expand_context=_settings.expand_context,
        include_archived=include_archived,
        exclude_categories=["obsidian"] if not include_obsidian else None,
    )

    if not results:
        return "검색 결과가 없습니다."

    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or ""
        section = r.get("section_path") or ""
        ci = r.get("chunk_index")
        score = r.get("rerank_score") if r.get("rerank_score") is not None else r.get("score", 0.0)
        header = f"[출처 {i} | {r['source_path']}"
        if ci is not None:
            header += f" #{ci}"
        header += f" | score {score:.3f} | 카테고리: {r['category']}"
        if title:
            header += f" | 제목: {title}"
        header += "]"
        section_line = f"섹션: {section}\n" if section else ""
        parts.append(f"{header}\n{section_line}{r['content']}\n")
    return "\n".join(parts)


@mcp.tool()
def write_file(file_path: str, content: str, ingest: bool = True) -> str:
    """파일을 작성하고 자동으로 ES에 인제스트합니다.
    data/ 하위 경로에만 저장 가능합니다 (.md만).

    최상위 폴더가 곧 카테고리입니다 — 기존 카테고리/주제 폴더를 우선 사용하고,
    새 최상위 폴더로 저장하면 새 카테고리가 됩니다.

    저장 전 내용을 다듬으세요 — 읽을 수 있는 산문(제텔카스텐 톤), frontmatter(title·tags·필요시
    expires_at) 포함.

    배치 결정: ① 기존 주제 폴더 우선 (list_documents로 확인) ② 단발 조사는
    data/study/daily-research/ ③ 계획된 시리즈만 새 폴더 ④ 같은 주제가 2~3건 쌓이면 폴더로
    승격. 날짜는 파일명이 아니라 frontmatter에.

    Args:
        file_path: 저장할 파일 경로 (예: data/writing/note.md)
        content: 파일에 작성할 내용
        ingest: True면 저장 후 바로 인제스트 (기본값 True)
    """
    full_path = _resolve_data_path(file_path)
    if full_path is None:
        return f"오류: data/ 하위 경로에만 파일을 작성할 수 있습니다. (입력: {file_path})"
    if full_path.suffix != ".md":
        return f"오류: 마크다운(.md) 파일만 작성 가능합니다. (입력: {file_path})"

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")

    result = f"파일 저장 완료: {file_path} ({len(content)}자)"
    if ingest:
        from pkb.config import data_dir
        from pkb.ingest import format_delta_stats, ingest_files

        stats = ingest_files([full_path], base_dir=data_dir(), doc_id_prefix="data/")
        result += f" | 인제스트: {format_delta_stats(stats)}"
    return result


@mcp.tool()
def list_documents(category: str = "", include_archived: bool = False, limit: int = 50) -> str:
    """저장된 문서 목록을 확인합니다. 기본적으로 아카이브된 문서는 제외.

    date_modified 내림차순으로 정렬해 상위 limit개만 표시합니다.

    Args:
        category: 카테고리 필터 = 코퍼스 최상위 폴더명. 빈 문자열이면 전체.
        include_archived: True면 아카이브된 문서도 표시. 기본 False.
        limit: 표시할 최대 문서 수. 0 이하이면 전체 표시. 기본 50.
    """
    from pkb.store import get_client
    from pkb.store import list_documents as _list_documents

    es = get_client()
    docs = _list_documents(es, category or None, include_archived=include_archived)

    if not docs:
        return "저장된 문서가 없습니다."

    docs.sort(key=lambda d: d.get("date_modified") or "", reverse=True)
    shown = docs if limit <= 0 else docs[:limit]

    lines = [f"총 {len(docs)}개 문서 (표시 {len(shown)}개 — limit·category로 조절)\n"]
    for doc in shown:
        lines.append(
            f"- {doc['doc_id']} [{doc['category']}] ({doc['chunks']}개 청크)"
        )
    return "\n".join(lines)


@mcp.tool()
def archive_document(doc_id: str, reason: str = "") -> str:
    """문서를 soft delete로 아카이브합니다 (검색에서 제외). 복구 가능.

    Args:
        doc_id: 아카이브할 문서의 doc_id (예: data/career/old_resume.md)
        reason: 아카이브 사유 (선택)
    """
    from pkb.store import archive_document as _archive
    from pkb.store import get_client

    es = get_client()
    try:
        n = _archive(es, doc_id, reason=reason or None)
    except Exception as e:
        return f"오류: {type(e).__name__}: {e}"
    if n == 0:
        return f"아카이브 대상 없음 (doc_id={doc_id})"
    msg = f"아카이브 완료: {doc_id} ({n}개 청크)"
    if reason:
        msg += f" | 사유: {reason}"
    return msg


@mcp.tool()
def restore_document(doc_id: str) -> str:
    """아카이브된 문서를 복구해 검색에 다시 노출합니다.

    Args:
        doc_id: 복구할 문서의 doc_id
    """
    from pkb.store import get_client
    from pkb.store import restore_document as _restore

    es = get_client()
    try:
        n = _restore(es, doc_id)
    except Exception as e:
        return f"오류: {type(e).__name__}: {e}"
    if n == 0:
        return f"복구할 아카이브 없음 (doc_id={doc_id})"
    return f"복구 완료: {doc_id} ({n}개 청크)"


@mcp.tool()
def add_document(file_path: str, tags: str = "") -> str:
    """파일을 지식 베이스에 인제스트합니다. md, txt, pdf, docx, pptx, xlsx, html 지원.

    Args:
        file_path: 인제스트할 파일 경로 (예: data/study/paper.pdf)
        tags: 쉼표 구분 태그 (예: python,backend)
    """
    from pkb.config import data_dir
    from pkb.ingest import (
        SUPPORTED_EXTENSIONS,
        format_delta_stats,
        ingest_files,
    )

    full_path = _resolve_data_path(file_path)
    if full_path is None:
        return f"오류: data/ 하위 경로만 인제스트할 수 있습니다. (입력: {file_path})"
    if not full_path.exists():
        return f"파일을 찾을 수 없습니다: {file_path}"
    if full_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return f"지원하지 않는 파일 형식입니다: {file_path} (지원: {sorted(SUPPORTED_EXTENSIONS)})"

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    stats = ingest_files(
        [full_path], base_dir=data_dir(), doc_id_prefix="data/", tag_override=tag_list
    )
    if not stats["files"]:
        return f"빈 파일입니다: {file_path}"
    return f"인제스트 완료: {file_path} — {format_delta_stats(stats)}"


@mcp.tool()
def convert_and_ingest(
    input_path: str,
    category: str,
    output_name: str = "",
    ingest: bool = True,
) -> str:
    """PDF/DOCX/PPTX/XLSX/HTML 파일을 마크다운으로 변환하여 data/에 저장하고 인제스트합니다.
    원본 파일은 어느 위치에 있어도 되며, 변환된 .md는 data/<category>/에 저장됩니다.

    **카테고리 = 코퍼스 최상위 폴더명 (동적).** 기존 카테고리를 우선 사용하고
    (전체 목록은 list_documents로 확인), 새 최상위 폴더명을 주면 새 카테고리가 생성됩니다.
    "study/payments"처럼 하위 폴더 경로도 지정 가능 — 카테고리는 첫 폴더(study)가 됩니다.
    기본 가이드: about(자기소개) / career(경력·프로젝트) / study(공부·논문) /
    writing(글 초안) / misc(그 외).

    Args:
        input_path: 변환할 원본 파일 경로 (절대경로 가능)
        category: 저장 위치 폴더. 내용을 보고 기존 주제 폴더를 우선 지정.
        output_name: 저장할 파일명 (확장자 제외). 빈 문자열이면 원본 파일명 사용.
        ingest: 변환 후 자동 인제스트 여부
    """
    from pathlib import Path

    from pkb.config import data_dir
    from pkb.ingest import (
        SUPPORTED_EXTENSIONS,
        format_delta_stats,
        ingest_files,
        read_file_as_text,
    )

    src = Path(input_path).expanduser().resolve()
    if not src.exists():
        return f"파일을 찾을 수 없습니다: {input_path}"
    if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return f"지원하지 않는 형식입니다: {src.suffix} (지원: {sorted(SUPPORTED_EXTENSIONS)})"
    if any(part.startswith(".") for part in Path(category).parts):
        return f"오류: 숨김 폴더에는 저장할 수 없습니다. (입력: {category})"

    text = read_file_as_text(src)

    data_root = data_dir()
    stem = output_name or src.stem
    output = (data_root / category / f"{stem}.md").resolve()

    if not output.is_relative_to(data_root):
        return f"오류: 저장 경로가 data/ 밖입니다. (카테고리/파일명 확인: {category}/{stem})"

    output.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- source: {src.name} | converted: {src.suffix} → .md -->\n\n"
    output.write_text(header + text, encoding="utf-8")

    result = f"변환 완료: data/{output.relative_to(data_root)} ({len(text)}자)"

    if ingest:
        stats = ingest_files([output], base_dir=data_root, doc_id_prefix="data/")
        if stats["files"]:
            result += f"\n인제스트 완료: {format_delta_stats(stats)}"

    return result


@mcp.tool()
def sync_obsidian(path: str = "", confirm_prune: bool = False) -> str:
    """Obsidian 볼트를 ES와 재조정(reconcile)합니다: 업서트 + 볼트에 없는 문서 정리.

    설정이 곧 기대 상태입니다 — OBSIDIAN_PATH가 비어 있으면 연동 해제로 보고
    잔존 obsidian/* 문서 전체 삭제를 제안합니다 (confirm_prune=True 필요).
    실시간 감시가 필요하면 별도 터미널에서 `uv run pkb watch`를 실행하세요.

    Args:
        path: Obsidian 볼트 절대경로. 빈 문자열이면 .env의 OBSIDIAN_PATH 사용.
        confirm_prune: 대량 정리(21개 이상 삭제) 승인. 소량 정리는 자동.
    """
    from pathlib import Path

    from pkb.config import data_dir, settings
    from pkb.ingest import format_delta_stats, reconcile
    from pkb.store import (
        PRUNE_CONFIRM_THRESHOLD,
        delete_document,
        get_client,
        list_doc_ids,
    )

    es = get_client()
    vault_path = path or settings.obsidian_path

    def _finish(msg: str) -> str:
        # sync 완료(ES 상태 변경)마다 그래프-코퍼스 재조정도 함께 반영.
        summary = _graph_prune_summary(es)
        return f"{msg}\n{summary}" if summary else msg

    # 연동 꺼짐 (켰다가 끔 케이스): 기대 상태 = 빈 집합
    if not vault_path:
        stale = sorted(list_doc_ids(es, "obsidian/"))
        if not stale:
            return _finish("Obsidian 연동 꺼짐 (OBSIDIAN_PATH 미설정) — 잔존 문서 없음.")
        if not confirm_prune:
            return (
                f"Obsidian 연동 꺼짐 + obsidian 문서 {len(stale)}개 잔존.\n"
                f"전부 삭제하려면 confirm_prune=True로 재호출하세요."
            )
        for doc_id in stale:
            delete_document(es, doc_id)
        return _finish(f"정리 완료: 잔존 obsidian 문서 {len(stale)}개 삭제 (연동 해제 반영)")

    vault = Path(vault_path).expanduser().resolve()
    if not vault.is_dir():
        return f"디렉터리를 찾을 수 없습니다 (정리하지 않음): {vault}"

    stats, stale = reconcile(
        es, vault, "obsidian/", category_override="obsidian", exclude=data_dir()
    )
    result = f"Obsidian 동기화: {format_delta_stats(stats)}\n경로: {vault}"

    # prune: 볼트에서 사라진 문서 정리
    if not stale:
        return _finish(result)
    if len(stale) > PRUNE_CONFIRM_THRESHOLD and not confirm_prune:
        preview = "\n".join(f"  - {d}" for d in stale[:10])
        return (
            f"{result}\n볼트에 없는 문서 {len(stale)}개 — 대량이라 삭제 보류:\n{preview}"
            f"\n  ...\n삭제하려면 confirm_prune=True로 재호출하세요. (경로 오설정 여부 먼저 확인)"
        )
    for doc_id in stale:
        delete_document(es, doc_id)
    return _finish(f"{result}\n정리: 볼트에서 사라진 문서 {len(stale)}개 삭제")


@mcp.tool()
def sync_corpus(confirm_prune: bool = False) -> str:
    """data/ 코퍼스 재조정+유령정리: 업서트 + 코퍼스에 없는 문서 정리.

    Args:
        confirm_prune: 대량 정리(21개 이상 삭제) 승인. 소량 정리는 자동.
    """
    from pkb.config import data_dir
    from pkb.ingest import format_delta_stats, reconcile
    from pkb.store import PRUNE_CONFIRM_THRESHOLD, delete_document, get_client

    es = get_client()
    root = data_dir()
    if not root.is_dir():
        return f"data 코퍼스 루트 없음 (정리하지 않음): {root}"

    stats, stale = reconcile(es, root, "data/")
    result = f"data 코퍼스 동기화: {format_delta_stats(stats)}\n경로: {root}"

    def _finish(msg: str) -> str:
        # sync 완료(ES 상태 변경)마다 그래프-코퍼스 재조정도 함께 반영.
        summary = _graph_prune_summary(es)
        return f"{msg}\n{summary}" if summary else msg

    if not stale:
        return _finish(result)
    if len(stale) > PRUNE_CONFIRM_THRESHOLD and not confirm_prune:
        preview = "\n".join(f"  - {d}" for d in stale[:10])
        return (
            f"{result}\n원본에 없는 문서 {len(stale)}개 — 대량이라 삭제 보류:\n{preview}"
            f"\n  ...\n삭제하려면 confirm_prune=True로 재호출하세요. (경로 오설정 여부 먼저 확인)"
        )
    for doc_id in stale:
        delete_document(es, doc_id)
    return _finish(f"{result}\n정리: 원본에 없는 문서 {len(stale)}개 삭제")


def _parse_chunk_range(chunk_range: str) -> tuple[int, int] | None:
    """"3" 또는 "3-7" → (start, end) 청크 인덱스(inclusive). 파싱 실패 시 None."""
    s = chunk_range.strip()
    if "-" in s:
        left, _, right = s.partition("-")
        try:
            start, end = int(left), int(right)
        except ValueError:
            return None
    else:
        try:
            start = end = int(s)
        except ValueError:
            return None
    if start < 0 or end < start:
        return None
    return start, end


def _render_document(
    doc_id: str, sources: list[dict], include_content: bool, chunk_range: str
) -> str:
    """문서 청크 목록을 사람이 읽을 텍스트로 렌더. get_document(mcp)/show(cli) 공용.

    우선순위: chunk_range(해당 청크만 전문) > include_content(전체 전문) > 목차만.
    """
    header = (
        f"문서: {doc_id} ({len(sources)}개 청크)\n"
        f"제목: {sources[0].get('title') or '-'} | 카테고리: {sources[0].get('category')} | "
        f"수정일: {sources[0].get('date_modified', '-')}"
    )

    def _full(selected: list[dict]) -> str:
        lines = [header]
        for src in selected:
            lines.append(f"\n[청크 #{src.get('chunk_index')}] section: {src.get('section_path', '-')}")
            lines.append(src.get("content", ""))
        return "\n".join(lines)

    if chunk_range:
        parsed = _parse_chunk_range(chunk_range)
        if parsed is None:
            return f'오류: 잘못된 chunk_range 형식입니다: "{chunk_range}" (예: "3" 또는 "3-7")'
        start, end = parsed
        selected = [s for s in sources if start <= s.get("chunk_index", -1) <= end]
        if not selected:
            return f"해당 범위의 청크가 없습니다: {chunk_range}"
        return _full(selected)

    if include_content:
        return _full(sources)

    lines = [header, ""]
    for src in sources:
        preview = (src.get("content") or "")[:60].replace("\n", " ")
        lines.append(f"#{src.get('chunk_index')} {src.get('section_path', '-')} — {preview}…")
    return "\n".join(lines)


@mcp.tool()
def get_document(doc_id: str, include_content: bool = False, chunk_range: str = "") -> str:
    """특정 문서를 조회합니다 (section_path 포함).

    기본(include_content=False)은 문서 메타(제목·카테고리·청크 수·수정일) + 청크 목차만
    반환합니다 — 큰 문서도 수천 자 수준입니다. 특정 구간만 필요하면 chunk_range로
    지정하세요 (예: "3" 또는 "3-7").

    Args:
        doc_id: 조회할 문서 ID (예: "data/study/rag/1.2.10_BM25.md")
        include_content: True면 전체 청크 전문을 반환합니다(하위호환). 대형 문서는
            수만 토큰이 될 수 있으므로 chunk_range 사용을 권장합니다.
        chunk_range: "3" 또는 "3-7" 형식으로 지정하면 해당 청크만 전문 반환.
    """
    from pkb.config import settings as _settings
    from pkb.store import get_client

    es = get_client()
    result = es.search(
        index=_settings.es_index,
        query={"term": {"doc_id": doc_id}},
        size=500,
        source_excludes=["embedding"],
        sort=[{"chunk_index": {"order": "asc"}}],
    )
    hits = result["hits"]["hits"]
    if not hits:
        return f"문서를 찾을 수 없습니다: {doc_id}"

    sources = [h["_source"] for h in hits]
    return _render_document(doc_id, sources, include_content, chunk_range)


@mcp.tool()
def reindex_document(doc_id: str) -> str:
    """특정 문서를 원본 파일로부터 재인제스트합니다 (수정 후 ES 동기화).

    Args:
        doc_id: 재인제스트할 문서 ID. doc_id가 "obsidian/..."으로 시작하면 OBSIDIAN_PATH 하위 파일로 처리.
    """
    from pathlib import Path

    from pkb.config import settings as _settings
    from pkb.ingest import format_delta_stats, ingest_files

    if doc_id.startswith("obsidian/"):
        if not _settings.obsidian_path:
            return "오류: OBSIDIAN_PATH가 설정되지 않았습니다."
        vault = Path(_settings.obsidian_path).expanduser().resolve()
        rel = doc_id[len("obsidian/"):]
        file_path = (vault / rel).resolve()
        base_dir = vault
        prefix = "obsidian/"
        cat = "obsidian"
    else:
        from pkb.config import data_dir

        base_dir = data_dir()
        file_path = _resolve_data_path(doc_id)
        if file_path is None:
            return f"오류: 알 수 없는 doc_id 형식입니다 (data/ 또는 obsidian/ 접두사 필요): {doc_id}"
        prefix = "data/"
        cat = None

    if not file_path.exists():
        return f"원본 파일을 찾을 수 없습니다: {file_path}"

    stats = ingest_files(
        [file_path], base_dir=base_dir, doc_id_prefix=prefix, category_override=cat
    )
    return f"재인제스트 완료: {doc_id} — {format_delta_stats(stats)}"


@mcp.tool()
def doctor() -> str:
    """PKB 시스템 상태 점검. ES 연결, 인덱스, 문서 수, 설정, 개념 그래프 통계 확인."""
    from pkb.report import build_health_report
    from pkb.store import get_client

    return build_health_report(get_client())


@mcp.tool()
def graph_list_chunks(
    category: str = "",
    doc_id: str = "",
    offset: int = 0,
    limit: int = 20,
) -> str:
    """개념 그래프 빌드를 위한 청크를 가져옵니다.

    **사용 방법**: 반환된 각 청크의 content를 직접 읽고 개념(concept)과 관계(relation)를 추출한 뒤
    `graph_store_concepts` 도구로 저장하세요. 이렇게 하면 별도 API 호출 없이
    Claude Code 세션이 그대로 추출기로 작동합니다.

    **추출 규칙**:
    - 개념(concept): 구체적 명사구 (예: "Dependency Injection", "BM25", "ReAct")
      일반 단어("방법","예시","내용")/인명/지명 제외
    - 각 개념: name, description(1~2문장 한국어), aliases(텍스트에 등장한 약어만)
    - 관계(relation): related_to | part_of | prerequisite_of | example_of 중 하나
      (필요 시 snake_case 자유 라벨도 허용)
    - 청크당 개념 8개·관계 12개 이내

    Args:
        category: 카테고리 필터 (study, obsidian, about, career, writing, misc)
        doc_id: 단일 문서 ID (category와 함께 사용 가능)
        offset: 페이지네이션 시작 위치
        limit: 반환 청크 수 (최대 50)

    Returns: 청크 목록 JSON + 다음 호출을 위한 next_offset
    """
    import json

    from pkb.config import settings as _settings
    from pkb.store import get_client

    if not category and not doc_id:
        return "오류: category 또는 doc_id 중 최소 하나는 지정해야 합니다."
    limit = max(1, min(limit, 50))

    es = get_client()
    filters = []
    if category:
        filters.append({"term": {"category": category}})
    if doc_id:
        filters.append({"term": {"doc_id": doc_id}})

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
    count_resp = es.count(index=_settings.es_index, query=query)
    total = count_resp["count"]

    result = es.search(
        index=_settings.es_index,
        query=query,
        size=limit,
        from_=offset,
        source_excludes=["embedding"],
        sort=[{"doc_id": "asc"}, {"chunk_index": "asc"}],
    )
    hits = result["hits"]["hits"]
    chunks = [
        {
            "doc_id": h["_source"]["doc_id"],
            "chunk_index": h["_source"]["chunk_index"],
            "category": h["_source"].get("category"),
            "title": h["_source"].get("title"),
            "section_path": h["_source"].get("section_path", ""),
            "content": h["_source"].get("content", ""),
        }
        for h in hits
    ]

    next_offset = offset + len(chunks)
    has_more = next_offset < total
    return json.dumps(
        {
            "total": total,
            "offset": offset,
            "returned": len(chunks),
            "next_offset": next_offset if has_more else None,
            "chunks": chunks,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def graph_store_concepts(items_json: str) -> str:
    """Claude Code가 추출한 개념/관계를 SQLite 그래프 DB에 저장합니다.

    Args:
        items_json: JSON 문자열. 스키마:
            {
              "items": [
                {
                  "doc_id": "obsidian/Spring/DI.md",
                  "chunk_index": 0,
                  "section_path": "...",
                  "category": "obsidian",
                  "title": "...",
                  "concepts": [
                    {"name": "Dependency Injection",
                     "aliases": ["DI"],
                     "description": "객체 간 의존성을 외부에서 주입..."}
                  ],
                  "relations": [
                    {"src": "Dependency Injection", "dst": "IoC", "type": "part_of"}
                  ]
                }
              ]
            }
    """
    import json

    from pkb.config import settings as _settings
    from pkb.embeddings import embed
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection, init_schema

    try:
        data = json.loads(items_json)
    except json.JSONDecodeError as e:
        return f"오류: JSON 파싱 실패: {e}"

    items = data.get("items") or []
    if not items:
        return "저장할 항목이 없습니다."

    init_schema(_settings.graph_db_path)
    conn = get_connection(_settings.graph_db_path)
    total_concepts = 0
    total_edges = 0
    total_mentions = 0

    try:
        for item in items:
            doc_id = item.get("doc_id")
            chunk_index = item.get("chunk_index")
            if not doc_id or chunk_index is None:
                continue

            gstore.upsert_document(
                conn,
                doc_id=doc_id,
                title=item.get("title"),
                category=item.get("category"),
            )

            concepts = item.get("concepts") or []
            if concepts:
                name_and_desc = [
                    f"{c.get('name','')}: {c.get('description','')}".strip(": ")
                    for c in concepts
                ]
                vecs = embed(name_and_desc) if name_and_desc else []

                name_to_id: dict[str, int] = {}
                for c, vec in zip(concepts, vecs, strict=False):
                    name = c.get("name", "").strip()
                    if not name:
                        continue
                    cid = gstore.upsert_concept(
                        conn,
                        name=name,
                        description=(c.get("description") or "").strip(),
                        category=item.get("category"),
                        embedding=vec,
                    )
                    total_concepts += 1
                    name_to_id[gstore.make_slug(name)] = cid
                    for alias in c.get("aliases", []) or []:
                        if isinstance(alias, str) and alias.strip():
                            gstore.add_alias(conn, cid, alias)
                    gstore.add_mention(
                        conn, cid, doc_id, int(chunk_index), item.get("section_path", "") or ""
                    )
                    total_mentions += 1

                for r in item.get("relations") or []:
                    src, dst, rtype = r.get("src"), r.get("dst"), r.get("type")
                    if not all(isinstance(x, str) and x.strip() for x in (src, dst, rtype)):
                        continue
                    src_id = name_to_id.get(gstore.make_slug(src))
                    dst_id = name_to_id.get(gstore.make_slug(dst))
                    if not src_id:
                        row = gstore.get_concept(conn, src)
                        src_id = row["id"] if row else None
                    if not dst_id:
                        row = gstore.get_concept(conn, dst)
                        dst_id = row["id"] if row else None
                    if src_id and dst_id and src_id != dst_id:
                        gstore.add_edge(conn, src_id, dst_id, rtype)
                        total_edges += 1

        conn.commit()
    finally:
        conn.close()

    return (
        f"저장 완료: 항목 {len(items)}개 처리, "
        f"개념 {total_concepts}개 / 관계 {total_edges}개 / 언급 {total_mentions}개 반영"
    )


@mcp.tool()
def sync_concept_notes(confirm_prune: bool = False) -> str:
    """SQLite 개념그래프를 data/concepts/<slug>.md 볼트 노트로 동기화합니다 (단방향, ES 미색인).

    개념 엣지를 [[위키링크]]로 노트에 되써서 Obsidian 그래프뷰가 개념그래프를 그리게 합니다.
    노트→SQLite 역승격은 없습니다 (SQLite가 항상 SSOT).

    Args:
        confirm_prune: 대량 정리(21개 이상 삭제) 승인. 소량 정리는 자동.
    """
    from pkb.config import settings as _settings
    from pkb.graph.notes import sync_concept_notes as _sync
    from pkb.graph.schema import get_connection, init_schema

    init_schema(_settings.graph_db_path)
    with get_connection(_settings.graph_db_path) as conn:
        result = _sync(conn, confirm_prune=confirm_prune)

    msg = (
        f"개념 노트 동기화: created={result['created']} updated={result['updated']} "
        f"skipped={result['skipped']} failed={result['failed']} pruned={result['pruned']}"
    )
    if result["pending_prune"]:
        preview = "\n".join(f"  - {p}" for p in result["pending_prune"][:10])
        msg += (
            f"\n정리 보류 {len(result['pending_prune'])}개 (대량이라 확인 필요):\n{preview}"
            f"\n  ...\n삭제하려면 confirm_prune=True로 재호출하세요."
        )
    return msg


def _warmup_background() -> None:
    """서버 기동 직후 백그라운드로 embedding/rerank 모델 + ES 경로를 예열.
    실패해도 서버 기동·정상 경로를 막지 않는다."""
    try:
        from pkb.retrieve import hybrid_search
        from pkb.store import get_client

        hybrid_search(
            get_client(),
            "warmup",
            top_k=1,
            rerank=True,
            log=False,
        )
    except Exception:
        pass


if __name__ == "__main__":
    import threading

    from pkb.config import settings as _settings

    if _settings.warmup_on_start:
        threading.Thread(target=_warmup_background, daemon=True).start()

    # 로컬 Claude Code가 stdio로 직접 기동한다.
    mcp.run()
