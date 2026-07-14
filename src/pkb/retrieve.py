from time import perf_counter

from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.embeddings import embed

RRF_K = 60  # Reciprocal Rank Fusion 상수 (Elastic 기본값)
MAX_CHUNKS_PER_DOC = 2  # 최종 결과에서 문서당 허용할 최대 청크 수 (다양성 캡)


def _cap_per_doc(
    candidates: list[dict], top_k: int, max_per_doc: int = MAX_CHUNKS_PER_DOC
) -> list[dict]:
    """정렬된 후보를 순회하며 doc_id별 max_per_doc개까지만 취해 top_k를 채운다.

    한 문서가 상위권을 독점하는 것을 막기 위한 다양성 캡. 캡 적용 후에도
    top_k에 미달하면, 캡에 걸려 건너뛴 후보들로 원래 순서대로 채운다.
    """
    counts: dict[str, int] = {}
    selected: list[dict] = []
    skipped: list[dict] = []
    for c in candidates:
        if len(selected) >= top_k:
            break
        doc_id = c.get("doc_id")
        count = counts.get(doc_id, 0)
        if count < max_per_doc:
            selected.append(c)
            counts[doc_id] = count + 1
        else:
            skipped.append(c)
    if len(selected) < top_k:
        selected.extend(skipped[: top_k - len(selected)])
    return selected


def _lifecycle_filter(include_archived: bool) -> list[dict]:
    """아카이브/만료 문서 제외 필터. include_archived=True면 빈 리스트 반환.

    기본 조건:
      - archived_at 필드가 없어야 함 (아카이브되지 않음)
      - expires_at이 없거나 현재 시점보다 미래여야 함
    """
    if include_archived:
        return []
    return [
        {"bool": {"must_not": {"exists": {"field": "archived_at"}}}},
        {
            "bool": {
                "should": [
                    {"bool": {"must_not": {"exists": {"field": "expires_at"}}}},
                    {"range": {"expires_at": {"gt": "now"}}},
                ],
                "minimum_should_match": 1,
            }
        },
    ]


def _exclude_doc_prefix_filter(exclude_doc_prefix: str | None) -> list[dict]:
    """제외할 doc_id 접두사(예: obsidian/) must_not prefix 필터. 없으면 빈 리스트."""
    if not exclude_doc_prefix:
        return []
    return [{"bool": {"must_not": [{"prefix": {"doc_id": exclude_doc_prefix}}]}}]


def _bm25_query(
    query_text: str,
    category: str | None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
) -> dict:
    bm25: dict = {
        "bool": {
            "should": [
                {"match": {"content": {"query": query_text, "boost": 1.0}}},
                {"match": {"title": {"query": query_text, "boost": 0.5}}},
                {"match": {"section_path": {"query": query_text, "boost": 0.3}}},
            ],
        }
    }
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    filters.extend(_lifecycle_filter(include_archived))
    filters.extend(_exclude_doc_prefix_filter(exclude_doc_prefix))
    if filters:
        bm25["bool"]["filter"] = filters
    return bm25


def _knn_query(
    query_vector: list[float],
    k: int,
    category: str | None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
) -> dict:
    knn: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": k * 5,
    }
    filters: list[dict] = []
    if category:
        filters.append({"term": {"category": category}})
    filters.extend(_lifecycle_filter(include_archived))
    filters.extend(_exclude_doc_prefix_filter(exclude_doc_prefix))
    if filters:
        knn["filter"] = filters
    return knn


def _source_to_dict(hit: dict) -> dict:
    source = hit["_source"]
    source["_id"] = hit["_id"]
    return source


def hybrid_search(
    es: Elasticsearch,
    query_text: str,
    category: str | None = None,
    top_k: int = 5,
    candidate_k: int = settings.candidate_k,
    rerank: bool = False,
    expand_context: int = 0,
    log: bool = True,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
    variants: list[str] | None = None,
) -> list[dict]:
    """하이브리드 검색.

    Args:
        top_k: 최종 반환 수
        candidate_k: 각 검색(BM25/kNN)에서 가져올 후보 수 (리랭크/RRF 용)
        rerank: True면 CrossEncoder 재순위 수행 후 top_k 반환
        expand_context: N>0이면 각 결과 전후 N개 청크를 neighbors 필드로 함께 반환
        exclude_doc_prefix: 지정하면 해당 doc_id 접두사 문서를 검색에서 제외 (예: "obsidian/")
        variants: 쿼리 변형(최대 3개) — 각 변형으로 검색해 RRF 점수를 _id별 합산 병합
            (RAG-Fusion). 리랭크는 원 query_text 기준 1회만 수행.
    """
    timings: dict[str, float] = {}
    t_total = perf_counter()

    # 원 쿼리 + 공백/중복 제거한 변형 최대 3개
    queries = [query_text]
    for v in variants or []:
        v = v.strip()
        if v and v not in queries and len(queries) < 4:
            queries.append(v)

    t = perf_counter()
    query_vectors = embed(queries)  # 변형 포함 1회 배치 인코딩
    timings["embed_ms"] = round((perf_counter() - t) * 1000, 2)

    fetch_k = candidate_k

    t = perf_counter()
    merged: dict[str, dict] = {}
    for i, (q, vec) in enumerate(zip(queries, query_vectors, strict=True)):
        hits = _rrf_search(
            es, q, vec, category, fetch_k,
            timings=timings if i == 0 else None,  # 세부 타이밍은 원 쿼리만 기록
            include_archived=include_archived,
            exclude_doc_prefix=exclude_doc_prefix,
        )
        for hit in hits:
            if hit["_id"] in merged:
                merged[hit["_id"]]["score"] += hit["score"]
            else:
                merged[hit["_id"]] = hit
    candidates = sorted(merged.values(), key=lambda h: -h["score"])
    timings["retrieve_ms"] = round((perf_counter() - t) * 1000, 2)

    if rerank:
        from pkb.rerank import rerank as _rerank_fn

        t = perf_counter()
        candidates = _rerank_fn(query_text, candidates, top_k=len(candidates))
        candidates = _cap_per_doc(candidates, top_k)
        timings["rerank_ms"] = round((perf_counter() - t) * 1000, 2)
    else:
        candidates = _cap_per_doc(candidates, top_k)

    if expand_context > 0:
        t = perf_counter()
        candidates = _attach_neighbors(es, candidates, window=expand_context)
        timings["expand_ms"] = round((perf_counter() - t) * 1000, 2)

    timings["total_ms"] = round((perf_counter() - t_total) * 1000, 2)

    if log:
        try:
            from pkb.search_log import log_search

            log_search(
                query=query_text,
                category=category,
                top_k=top_k,
                fusion="rrf",
                reranked=rerank,
                results=candidates,
                latency_ms=timings,
                variants=queries[1:],
            )
        except Exception:
            pass  # 로깅 실패는 검색을 막지 않음

    return candidates


def _attach_neighbors(
    es: Elasticsearch, hits: list[dict], window: int = 1
) -> list[dict]:
    """각 hit의 전후 window개 청크를 neighbors 필드로 부착 (동일 doc_id 내).
    검색 결과를 상위 맥락과 함께 반환할 때 사용."""
    for hit in hits:
        doc_id = hit.get("doc_id")
        ci = hit.get("chunk_index")
        if doc_id is None or ci is None:
            hit["neighbors"] = []
            continue

        start = max(0, ci - window)
        end = ci + window
        result = es.search(
            index=settings.es_index,
            query={
                "bool": {
                    "must": [
                        {"term": {"doc_id": doc_id}},
                        {"range": {"chunk_index": {"gte": start, "lte": end}}},
                    ]
                }
            },
            size=window * 2 + 1,
            source_excludes=["embedding"],
            sort=[{"chunk_index": {"order": "asc"}}],
        )
        neighbors = []
        for nh in result["hits"]["hits"]:
            src = nh["_source"]
            if src.get("chunk_index") == ci:
                continue  # 자기 자신 제외
            neighbors.append(
                {
                    "chunk_index": src.get("chunk_index"),
                    "section_path": src.get("section_path"),
                    "content": src.get("content"),
                }
            )
        hit["neighbors"] = neighbors
    return hits


def _rrf_search(
    es: Elasticsearch,
    query_text: str,
    query_vector: list[float],
    category: str | None,
    candidate_k: int,
    timings: dict[str, float] | None = None,
    include_archived: bool = False,
    exclude_doc_prefix: str | None = None,
) -> list[dict]:
    """BM25와 kNN을 각각 실행 → Reciprocal Rank Fusion으로 결합.

    timings이 주어지면 bm25_ms/knn_ms/fusion_ms/candidate_count/rrf_top_gap 기록.
    include_archived=False(기본)면 archived/expired 문서는 검색에서 제외.
    exclude_doc_prefix가 주어지면 해당 doc_id 접두사 문서도 검색에서 제외.
    """
    t = perf_counter()
    bm25_result = es.search(
        index=settings.es_index,
        query=_bm25_query(
            query_text, category, include_archived=include_archived,
            exclude_doc_prefix=exclude_doc_prefix,
        ),
        size=candidate_k,
        source_excludes=["embedding"],
    )
    if timings is not None:
        timings["bm25_ms"] = round((perf_counter() - t) * 1000, 2)

    t = perf_counter()
    knn_result = es.search(
        index=settings.es_index,
        knn=_knn_query(
            query_vector, candidate_k, category, include_archived=include_archived,
            exclude_doc_prefix=exclude_doc_prefix,
        ),
        size=candidate_k,
        source_excludes=["embedding"],
    )
    if timings is not None:
        timings["knn_ms"] = round((perf_counter() - t) * 1000, 2)

    t = perf_counter()
    # doc_id(_id) → {rrf_score, source}
    combined: dict[str, dict] = {}
    for rank, hit in enumerate(bm25_result["hits"]["hits"]):
        doc_id = hit["_id"]
        rrf = 1.0 / (RRF_K + rank + 1)
        combined[doc_id] = {"score": rrf, "source": _source_to_dict(hit)}

    for rank, hit in enumerate(knn_result["hits"]["hits"]):
        doc_id = hit["_id"]
        rrf = 1.0 / (RRF_K + rank + 1)
        if doc_id in combined:
            combined[doc_id]["score"] += rrf
        else:
            combined[doc_id] = {"score": rrf, "source": _source_to_dict(hit)}

    sorted_hits = sorted(combined.values(), key=lambda x: -x["score"])
    results = []
    for item in sorted_hits:
        source = item["source"]
        source["score"] = item["score"]
        results.append(source)

    if timings is not None:
        timings["fusion_ms"] = round((perf_counter() - t) * 1000, 2)
        timings["candidate_count"] = len(sorted_hits)
        if len(sorted_hits) >= 2:
            timings["rrf_top_gap"] = round(
                sorted_hits[0]["score"] - sorted_hits[1]["score"], 6
            )
        else:
            timings["rrf_top_gap"] = 0.0
    return results
