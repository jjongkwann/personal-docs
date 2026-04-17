"""PKB MCP Server — Claude Code에서 개인 지식 베이스에 직접 접근."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pkb", instructions="""개인 지식 관리 시스템(PKB)의 기본 인터페이스입니다.
사용자의 개인 데이터(경력, 공부 노트, 자기소개, Obsidian 등)가 Elasticsearch에 저장되어 있습니다.
질문에 답하려면 search_knowledge로 검색하고, 파일 작성은 write_file을 사용하세요.
개념 관계 질문은 search_concepts, explain_concept, related_concepts를 우선 사용하세요.""")


@mcp.tool()
def search_knowledge(query: str, category: str = "", top_k: int = 5) -> str:
    """개인 지식 베이스에서 관련 정보를 하이브리드 검색(BM25+kNN)합니다.
    RRF 결합 + CrossEncoder 재순위로 정밀도를 높입니다.

    Args:
        query: 검색할 질문 또는 키워드
        category: 카테고리 필터 (about/career/study/writing/obsidian). 빈 문자열이면 전체.
        top_k: 반환할 결과 수
    """
    from pkb.config import settings as _settings
    from pkb.retrieve import hybrid_search
    from pkb.store import get_client

    es = get_client()
    results = hybrid_search(
        es, query,
        category=category or None, top_k=top_k,
        candidate_k=_settings.candidate_k,
        fusion=_settings.fusion,
        rerank=_settings.rerank_enabled,
        expand_context=_settings.expand_context,
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

    Args:
        file_path: 저장할 파일 경로 (예: data/writing/note.md)
        content: 파일에 작성할 내용
        ingest: True면 저장 후 바로 인제스트 (기본값 True)
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

    result = f"파일 저장 완료: {file_path} ({len(content)}자)"
    if ingest:
        from pkb.ingest import ingest_files

        count = ingest_files([full_path], base_dir=Path.cwd())
        result += f" | 인제스트: {count}개 청크"
    return result


@mcp.tool()
def list_documents(category: str = "") -> str:
    """저장된 문서 목록을 확인합니다.

    Args:
        category: 카테고리 필터 (about, career, study, writing, obsidian, misc). 빈 문자열이면 전체.
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
    for chunk, vector in zip(chunks, vectors, strict=False):
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
            for chunk, vector in zip(chunks, vectors, strict=False):
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


@mcp.tool()
def get_document(doc_id: str, include_content: bool = True) -> str:
    """특정 문서의 모든 청크를 조회합니다 (section_path 포함).

    Args:
        doc_id: 조회할 문서 ID (예: "data/study/rag/1.2.10_BM25.md")
        include_content: True면 청크 본문 포함, False면 메타데이터만
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

    lines = [f"문서: {doc_id} ({len(hits)}개 청크)"]
    for hit in hits:
        src = hit["_source"]
        lines.append(f"\n[청크 #{src.get('chunk_index')}] section: {src.get('section_path', '-')}")
        if include_content:
            lines.append(src.get("content", ""))
    return "\n".join(lines)


@mcp.tool()
def reindex_document(doc_id: str) -> str:
    """특정 문서를 원본 파일로부터 재인제스트합니다 (수정 후 ES 동기화).

    Args:
        doc_id: 재인제스트할 문서 ID. doc_id가 "obsidian/..."으로 시작하면 OBSIDIAN_PATH 하위 파일로 처리.
    """
    from pathlib import Path

    from pkb.config import settings as _settings
    from pkb.ingest import ingest_files

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
        base_dir = Path.cwd()
        file_path = (base_dir / doc_id).resolve()
        prefix = ""
        cat = None

    if not file_path.exists():
        return f"원본 파일을 찾을 수 없습니다: {file_path}"

    count = ingest_files(
        [file_path], base_dir=base_dir, doc_id_prefix=prefix, category_override=cat
    )
    return f"재인제스트 완료: {doc_id} ({count}개 청크)"


@mcp.tool()
def doctor() -> str:
    """PKB 시스템 상태 점검. ES 연결, 인덱스, 문서 수, 설정 확인."""
    from pkb.config import settings as _settings
    from pkb.store import count_documents, get_client

    lines = ["=== PKB Doctor ==="]

    # ES 연결
    try:
        es = get_client()
        info = es.info()
        lines.append(f"ES: {info['version']['number']} ({_settings.es_host})")
    except Exception as e:
        lines.append(f"ES: 연결 실패 — {e}")
        return "\n".join(lines)

    # 인덱스
    try:
        if es.indices.exists(index=_settings.es_index):
            count = count_documents(es)
            lines.append(f"인덱스 '{_settings.es_index}': {count}개 청크")

            # 카테고리별 집계
            agg = es.search(
                index=_settings.es_index,
                size=0,
                aggs={"by_cat": {"terms": {"field": "category", "size": 20}}},
            )
            for bucket in agg["aggregations"]["by_cat"]["buckets"]:
                lines.append(f"  - {bucket['key']}: {bucket['doc_count']}")
        else:
            lines.append(f"인덱스 '{_settings.es_index}': 없음. `pkb init` 필요")
    except Exception as e:
        lines.append(f"인덱스 조회 실패: {e}")

    # 설정
    lines.append("\n=== 설정 ===")
    lines.append(f"embedding_model: {_settings.embedding_model}")
    lines.append(f"rerank_model: {_settings.rerank_model}")
    lines.append(f"rerank_enabled: {_settings.rerank_enabled}")
    lines.append(f"fusion: {_settings.fusion}")
    lines.append(f"candidate_k: {_settings.candidate_k}")
    lines.append(f"chunk_size: {_settings.chunk_size}, overlap: {_settings.chunk_overlap}")
    lines.append(f"obsidian_path: {_settings.obsidian_path or '(미설정)'}")

    return "\n".join(lines)


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
def search_concepts(query: str, top_k: int = 10) -> str:
    """개념 그래프에서 질의 임베딩과 유사한 개념을 검색합니다.

    Args:
        query: 검색 질의
        top_k: 반환할 개념 수
    """
    from pkb.config import settings as _settings
    from pkb.embeddings import embed
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection

    vec = embed([query])[0]
    with get_connection(_settings.graph_db_path) as conn:
        results = gstore.search_concepts_by_embedding(conn, vec, top_k=top_k)

    if not results:
        return (
            "개념이 없습니다. MCP에서는 graph_list_chunks로 청크를 읽고 "
            "graph_store_concepts로 개념/관계를 저장하세요. "
            "일괄 빌드는 `uv run pkb graph build --category <category>`를 사용할 수 있습니다."
        )

    lines = [f"유사 개념 top-{len(results)}:"]
    for row, score in results:
        lines.append(
            f"- [{score:.3f}] {row['name']} ({row['category'] or '-'}) "
            f"mentions={row['mention_count']} | {row['description'] or ''}"
        )
    return "\n".join(lines)


@mcp.tool()
def explain_concept(name: str, depth: int = 1) -> str:
    """개념의 설명, 이웃 개념(관계), 언급된 문서를 조회합니다.

    Args:
        name: 개념 이름 (별칭도 인식)
        depth: 1=직접 이웃만, 2=이웃의 이웃까지 BFS
    """
    from pkb.config import settings as _settings
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection

    with get_connection(_settings.graph_db_path) as conn:
        row = gstore.get_concept(conn, name)
        if not row:
            return f"개념을 찾을 수 없습니다: {name}"

        lines = [f"# {row['name']}"]
        if row["description"]:
            lines.append(row["description"])
        lines.append(f"\ncategory: {row['category'] or '-'}, mentions: {row['mention_count']}")

        # 1-hop 관계 (outbound + inbound)
        out_edges = gstore.list_edges(conn, row["id"])
        in_edges = gstore.list_inbound_edges(conn, row["id"])
        if out_edges:
            lines.append(f"\n## 직접 관계 — 나가는 엣지 ({len(out_edges)}개)")
            for e in out_edges:
                lines.append(
                    f"- [{e['relation']}] → {e['dst_name']} (weight={e['weight']:.1f})"
                )
        if in_edges:
            lines.append(f"\n## 직접 관계 — 들어오는 엣지 ({len(in_edges)}개)")
            for e in in_edges:
                lines.append(
                    f"- {e['src_name']} → [{e['relation']}] (weight={e['weight']:.1f})"
                )

        # 2-hop (outbound만)
        if depth >= 2 and out_edges:
            lines.append("\n## 2-hop 이웃 (outbound)")
            seen = {row["id"]} | {e["dst_id"] for e in out_edges}
            for e in out_edges:
                sub = gstore.list_edges(conn, e["dst_id"])
                for s in sub:
                    if s["dst_id"] in seen:
                        continue
                    seen.add(s["dst_id"])
                    lines.append(
                        f"- {e['dst_name']} → [{s['relation']}] → {s['dst_name']}"
                    )

        # 언급 문서
        mentions = gstore.list_mentions(conn, row["id"], limit=10)
        if mentions:
            lines.append(f"\n## 언급 문서 (상위 {len(mentions)})")
            for m in mentions:
                lines.append(
                    f"- {m['doc_id']} #chunk{m['chunk_index']}"
                    + (f" ({m['section_path']})" if m["section_path"] else "")
                )

        return "\n".join(lines)


@mcp.tool()
def related_concepts(
    name: str, relation: str = "", direction: str = "both", top_k: int = 20
) -> str:
    """특정 개념의 직접 이웃을 조회 (outbound + inbound).

    Args:
        name: 개념 이름 또는 별칭
        relation: 관계 타입 필터 (예: part_of). 빈 문자열이면 전체.
        direction: "out" | "in" | "both" (기본 both)
        top_k: 각 방향별 최대 반환 수
    """
    from pkb.config import settings as _settings
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection

    with get_connection(_settings.graph_db_path) as conn:
        row = gstore.get_concept(conn, name)
        if not row:
            return f"개념을 찾을 수 없습니다: {name}"

        out_edges = []
        in_edges = []
        if direction in ("out", "both"):
            out_edges = gstore.list_edges(conn, row["id"], relation=relation or None)[:top_k]
        if direction in ("in", "both"):
            in_edges = gstore.list_inbound_edges(
                conn, row["id"], relation=relation or None
            )[:top_k]

    if not out_edges and not in_edges:
        return f"{row['name']}의 이웃이 없습니다."

    lines = [f"{row['name']}의 이웃:"]
    if out_edges:
        lines.append(f"\n## Outbound ({len(out_edges)}개)")
        for e in out_edges:
            lines.append(f"- [{e['relation']}] → {e['dst_name']} (weight={e['weight']:.1f})")
    if in_edges:
        lines.append(f"\n## Inbound ({len(in_edges)}개)")
        for e in in_edges:
            lines.append(f"- {e['src_name']} → [{e['relation']}] (weight={e['weight']:.1f})")
    return "\n".join(lines)


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

    mcp.run()
