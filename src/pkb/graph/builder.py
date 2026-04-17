"""개념 그래프 빌드 오케스트레이션.

ES에서 청크를 스캔 → LLM 추출 → SQLite에 저장.
"""

from typing import Iterator

from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.graph import store
from pkb.graph.extract import extract_from_chunk
from pkb.graph.schema import get_connection, init_schema


def _iter_chunks(
    es: Elasticsearch,
    category: str = "",
    doc_id: str = "",
    batch_size: int = 200,
) -> Iterator[dict]:
    """scope에 해당하는 청크를 스크롤로 순회."""
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    if doc_id:
        filters.append({"term": {"doc_id": doc_id}})

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}

    resp = es.search(
        index=settings.es_index,
        query=query,
        size=batch_size,
        source_excludes=["embedding"],
        scroll="2m",
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                break
            for hit in hits:
                yield hit["_source"]
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass


def estimate_cost(chunk_count: int) -> str:
    """비용 추정 (Haiku 기준). 매우 대략."""
    # 입력 ~1500 tok, 출력 ~300 tok × chunk_count
    in_tok = chunk_count * 1500
    out_tok = chunk_count * 300
    # Haiku 4.5 대략 $1/1M in, $5/1M out (추정; 실제 가격은 Anthropic 공식 참조)
    cost = in_tok / 1_000_000 * 1.0 + out_tok / 1_000_000 * 5.0
    return f"약 ${cost:.2f} (청크 {chunk_count}개, 입력~{in_tok:,}tok / 출력~{out_tok:,}tok 추정)"


def build(
    es: Elasticsearch,
    category: str = "",
    doc_id: str = "",
    rebuild: bool = False,
    progress_cb=None,
) -> dict:
    """주어진 scope에서 개념 그래프 빌드.

    Args:
        category / doc_id: 최소 하나는 필수
        rebuild: True면 기존 scope 데이터 제거 후 재빌드
        progress_cb(idx, total, current_doc_id): 진행률 콜백

    Returns: {chunks_processed, concepts_added, edges_added, status}
    """
    if not category and not doc_id:
        raise ValueError("category 또는 doc_id 중 최소 하나는 지정해야 합니다.")

    init_schema(settings.graph_db_path)

    # 청크 개수 사전 조회
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    if doc_id:
        filters.append({"term": {"doc_id": doc_id}})
    pre_count = es.count(
        index=settings.es_index,
        query={"bool": {"filter": filters}} if filters else {"match_all": {}},
    )
    total_chunks = pre_count["count"]

    from pkb.embeddings import embed  # 지연 import

    conn = get_connection(settings.graph_db_path)
    run_id = store.start_run(conn, category, doc_id, settings.graph_extract_model)
    conn.commit()

    before = store.stats(conn)
    chunks_processed = 0

    try:
        # rebuild: 해당 scope의 기존 mentions 제거 (개념 노드는 유지; 다른 scope와 공유 가능하므로)
        if rebuild:
            if doc_id:
                conn.execute("DELETE FROM concept_mentions WHERE doc_id = ?", (doc_id,))
            elif category:
                conn.execute(
                    "DELETE FROM concept_mentions WHERE doc_id IN "
                    "(SELECT doc_id FROM documents WHERE category = ?)",
                    (category,),
                )
            conn.commit()

        for chunk in _iter_chunks(es, category=category, doc_id=doc_id):
            chunks_processed += 1
            if progress_cb:
                progress_cb(chunks_processed, total_chunks, chunk.get("doc_id"))

            content = chunk.get("content", "")
            if not content.strip():
                continue

            # 문서 upsert
            store.upsert_document(
                conn,
                doc_id=chunk["doc_id"],
                title=chunk.get("title"),
                category=chunk.get("category"),
            )

            # LLM 추출
            concepts, relations = extract_from_chunk(content)
            if not concepts:
                continue

            # 개념 임베딩 (이름 + 설명)
            names_for_embed = [
                f"{c.get('name', '')}: {c.get('description', '')}".strip(": ")
                for c in concepts
            ]
            embeddings = embed(names_for_embed) if names_for_embed else []

            name_to_id: dict[str, int] = {}
            for c, emb in zip(concepts, embeddings):
                cid = store.upsert_concept(
                    conn,
                    name=c["name"],
                    description=c.get("description", "") or "",
                    category=chunk.get("category"),
                    embedding=emb,
                )
                name_to_id[store.make_slug(c["name"])] = cid
                for alias in c.get("aliases", []) or []:
                    if isinstance(alias, str) and alias.strip():
                        store.add_alias(conn, cid, alias)
                store.add_mention(
                    conn, cid, chunk["doc_id"], chunk["chunk_index"], chunk.get("section_path") or ""
                )

            # 관계
            for r in relations:
                src_slug = store.make_slug(r["src"])
                dst_slug = store.make_slug(r["dst"])
                src_id = name_to_id.get(src_slug)
                dst_id = name_to_id.get(dst_slug)
                # 이번 청크에 없는 이름은 기존 개념에서 조회
                if not src_id:
                    row = store.get_concept(conn, r["src"])
                    src_id = row["id"] if row else None
                if not dst_id:
                    row = store.get_concept(conn, r["dst"])
                    dst_id = row["id"] if row else None
                if src_id and dst_id and src_id != dst_id:
                    store.add_edge(conn, src_id, dst_id, r["type"])

            # 주기적 커밋
            if chunks_processed % 10 == 0:
                conn.commit()

        conn.commit()
        after = store.stats(conn)
        concepts_added = after["concepts"] - before["concepts"]
        edges_added = after["edges"] - before["edges"]
        store.finish_run(conn, run_id, chunks_processed, concepts_added, edges_added, "success")
        conn.commit()
        return {
            "chunks_processed": chunks_processed,
            "concepts_added": concepts_added,
            "edges_added": edges_added,
            "status": "success",
        }
    except Exception as e:
        store.finish_run(conn, run_id, chunks_processed, 0, 0, f"failed: {e}")
        conn.commit()
        raise
    finally:
        conn.close()
