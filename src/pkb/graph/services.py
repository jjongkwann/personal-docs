"""SQLite 그래프와 Elasticsearch 청크를 함께 다루는 서비스."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.graph import store as graph_store

SCAN_PAGE_SIZE = 2000


def scan_pending_chunks(
    es: Elasticsearch,
    conn: sqlite3.Connection,
    *,
    query: dict | None = None,
) -> tuple[list[dict], int]:
    """ES 청크를 search_after로 전량 순회해 (pending source, 전체 수)를 반환."""
    by_idx, legacy = graph_store.extracted_markers(conn)
    pending: list[dict] = []
    total = 0
    search_after = None
    while True:
        scan = es.search(
            index=settings.es_index,
            query=query or {"match_all": {}},
            size=SCAN_PAGE_SIZE,
            source_includes=["doc_id", "chunk_index", "content_hash"],
            sort=[{"doc_id": "asc"}, {"chunk_index": "asc"}],
            **({"search_after": search_after} if search_after else {}),
        )
        hits = scan["hits"]["hits"]
        if not hits:
            break
        total += len(hits)
        pending.extend(
            source
            for source in (hit["_source"] for hit in hits)
            if graph_store.is_pending(source, by_idx, legacy)
        )
        search_after = hits[-1]["sort"]
    return pending, total


def load_pending_batch(
    es: Elasticsearch,
    conn: sqlite3.Connection,
    *,
    limit: int,
    query: dict | None = None,
) -> tuple[list[dict], int, int]:
    """pending 앞쪽 배치의 본문을 mget으로 읽어 (chunks, pending, total) 반환."""
    pending, total = scan_pending_chunks(es, conn, query=query)
    page = pending[:limit]
    if not page:
        return [], len(pending), total
    docs = es.mget(
        index=settings.es_index,
        ids=[f"{source['doc_id']}_{source['chunk_index']}" for source in page],
        source_excludes=["embedding"],
    )["docs"]
    chunks = [
        {
            "doc_id": doc["_source"]["doc_id"],
            "chunk_index": doc["_source"]["chunk_index"],
            "category": doc["_source"].get("category"),
            "title": doc["_source"].get("title"),
            "section_path": doc["_source"].get("section_path", ""),
            "content": doc["_source"].get("content", ""),
        }
        for doc in docs
        if doc.get("found")
    ]
    return chunks, len(pending), total


def legacy_concept_hints(
    conn: sqlite3.Connection, keys: list[tuple[str, int]]
) -> dict[tuple[str, int], list[str]]:
    """staging 재구축 중 기존 멘션의 canonical concept 이름을 청크별 힌트로 반환."""
    hints: dict[tuple[str, int], list[str]] = {key: [] for key in keys}
    for doc_id, chunk_index in keys:
        rows = conn.execute(
            "SELECT c.name FROM concept_mentions m "
            "JOIN concepts c ON c.id = m.concept_id "
            "WHERE m.doc_id = ? AND m.chunk_index = ? ORDER BY c.name",
            (doc_id, chunk_index),
        ).fetchall()
        hints[(doc_id, chunk_index)] = [row["name"] for row in rows]
    return hints


def _chunk_hashes(keys: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """(doc_id, chunk_index) → 현재 content_hash. ES 조회 실패는 빈 dict."""
    if not keys:
        return {}
    try:
        from pkb.store import get_client

        docs = get_client().mget(
            index=settings.es_index,
            ids=[f"{doc_id}_{index}" for doc_id, index in keys],
            source_includes=["content_hash"],
        )["docs"]
    except Exception:
        return {}
    hashes = {}
    for key, doc in zip(keys, docs, strict=False):
        content_hash = (
            (doc.get("_source") or {}).get("content_hash") if doc.get("found") else None
        )
        if content_hash:
            hashes[key] = content_hash
    return hashes


def store_concepts(items_json: str) -> str:
    """추출된 개념/관계 JSON을 증분·evidence 규칙에 맞춰 그래프에 저장."""
    from pkb.embeddings import embed
    from pkb.graph.schema import graph_connection

    try:
        data = json.loads(items_json)
    except json.JSONDecodeError as exc:
        return f"오류: JSON 파싱 실패: {exc}"

    items = data.get("items") or []
    if not items:
        return "저장할 항목이 없습니다."

    total_concepts = 0
    total_edges = 0
    total_mentions = 0
    dropped: list[tuple[str, str, str]] = []
    invalid_concepts: list[str] = []
    processed: list[tuple[str, int]] = []
    touched: set[int] = set()
    keys = [
        (item["doc_id"], int(item["chunk_index"]))
        for item in items
        if item.get("doc_id") and item.get("chunk_index") is not None
    ]
    current_hashes = _chunk_hashes(keys)

    with graph_connection(settings.graph_db_path) as conn:
        marker_hashes, _ = graph_store.extracted_markers(conn)
        materialize_edges = not graph_store.edge_evidence_rebuild_active(conn)
        for item in items:
            doc_id = item.get("doc_id")
            chunk_index = item.get("chunk_index")
            if not doc_id or chunk_index is None:
                continue
            index = int(chunk_index)
            key = (doc_id, index)
            processed.append(key)

            current_hash = current_hashes.get(key)
            if current_hash is not None and marker_hashes.get(key) != current_hash:
                touched.update(graph_store.clear_mentions_for_chunk(conn, doc_id, index))
                graph_store.clear_edge_evidence_for_chunk(conn, doc_id, index)

            graph_store.upsert_document(
                conn,
                doc_id=doc_id,
                title=item.get("title"),
                category=item.get("category"),
            )

            concepts = item.get("concepts") or []
            name_to_id: dict[str, int] = {}
            if concepts:
                valid_concepts = []
                for concept in concepts:
                    name = concept.get("name", "")
                    name = name.strip() if isinstance(name, str) else ""
                    if not name or not graph_store.make_slug(name):
                        invalid_concepts.append(repr(concept.get("name")))
                        continue
                    valid_concepts.append(concept)
                names_and_descriptions = [
                    f"{concept.get('name', '')}: {concept.get('description', '')}".strip(": ")
                    for concept in valid_concepts
                ]
                vectors = embed(names_and_descriptions) if names_and_descriptions else []

                for concept, vector in zip(valid_concepts, vectors, strict=False):
                    name = concept.get("name", "").strip()
                    concept_id = graph_store.upsert_concept(
                        conn,
                        name=name,
                        description=(concept.get("description") or "").strip(),
                        category=item.get("category"),
                        embedding=vector,
                        match_by_alias=False,
                        match_by_embedding=False,
                    )
                    total_concepts += 1
                    name_to_id[graph_store.make_slug(name)] = concept_id
                    for alias in concept.get("aliases", []) or []:
                        if isinstance(alias, str) and alias.strip():
                            graph_store.add_alias(conn, concept_id, alias)
                    graph_store.add_mention(
                        conn,
                        concept_id,
                        doc_id,
                        index,
                        item.get("section_path", "") or "",
                    )
                    touched.add(concept_id)
                    total_mentions += 1

            for relation in item.get("relations") or []:
                src = relation.get("src")
                dst = relation.get("dst")
                relation_type = relation.get("type")
                if not all(
                    isinstance(value, str) and value.strip()
                    for value in (src, dst, relation_type)
                ):
                    continue
                src_id = name_to_id.get(graph_store.make_slug(src))
                dst_id = name_to_id.get(graph_store.make_slug(dst))
                if not src_id:
                    row = graph_store.get_concept(conn, src)
                    src_id = row["id"] if row else None
                if not dst_id:
                    row = graph_store.get_concept(conn, dst)
                    dst_id = row["id"] if row else None
                if src_id and dst_id:
                    if src_id != dst_id:
                        confidence = relation.get("confidence")
                        if confidence not in (0.9, 0.7, 0.5):
                            confidence = None
                        graph_store.add_edge(
                            conn,
                            src_id,
                            dst_id,
                            relation_type,
                            confidence=confidence,
                            doc_id=doc_id,
                            chunk_index=index,
                            materialize=materialize_edges,
                        )
                        total_edges += 1
                else:
                    dropped.append((src, dst, relation_type))

        now = datetime.now(UTC).isoformat()
        for key in processed:
            content_hash = current_hashes.get(key)
            if content_hash:
                graph_store.record_extraction(conn, key[0], key[1], content_hash, now)
        graph_store.recompute_mention_counts(conn, touched)

    message = (
        f"저장 완료: 항목 {len(items)}개 처리, "
        f"개념 {total_concepts}개 / 관계 {total_edges}개 / 언급 {total_mentions}개 반영"
    )
    if dropped:
        preview = ", ".join(f"{src}→{dst}({kind})" for src, dst, kind in dropped[:10])
        message += (
            f"\n관계 {len(dropped)}건 미해소: {preview}"
            f" — 누락 개념과 미해소 관계만 담은 items로 재호출하세요"
        )
    if invalid_concepts:
        message += (
            f"\n빈 slug 개념 {len(invalid_concepts)}건 제외: "
            f"{', '.join(invalid_concepts[:10])}"
        )
    return message
