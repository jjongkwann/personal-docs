from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.embeddings import embed

RRF_K = 60  # Reciprocal Rank Fusion 상수 (Elastic 기본값)


def _bm25_query(query_text: str, category: str | None) -> dict:
    bm25: dict = {
        "bool": {
            "should": [
                {"match": {"content": {"query": query_text, "boost": 1.0}}},
                {"match": {"title": {"query": query_text, "boost": 0.5}}},
            ],
        }
    }
    if category:
        bm25["bool"]["filter"] = [{"term": {"category": category}}]
    return bm25


def _knn_query(query_vector: list[float], k: int, category: str | None) -> dict:
    knn: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": k,
        "num_candidates": k * 5,
    }
    if category:
        knn["filter"] = [{"term": {"category": category}}]
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
    candidate_k: int = 50,
    fusion: str = "rrf",
    rerank: bool = False,
    log: bool = True,
) -> list[dict]:
    """하이브리드 검색.

    Args:
        top_k: 최종 반환 수
        candidate_k: 각 검색(BM25/kNN)에서 가져올 후보 수 (리랭크/RRF 용)
        fusion: "rrf" (BM25/kNN 분리 후 Reciprocal Rank Fusion) 또는 "native" (ES에 같이 넣어 자동 합산)
        rerank: True면 CrossEncoder 재순위 수행 후 top_k 반환
    """
    query_vector = embed([query_text])[0]
    fetch_k = candidate_k if (rerank or fusion == "rrf") else top_k

    if fusion == "rrf":
        candidates = _rrf_search(
            es, query_text, query_vector, category, fetch_k
        )
    else:
        candidates = _native_search(
            es, query_text, query_vector, category, fetch_k
        )

    if rerank:
        from pkb.rerank import rerank as _rerank_fn

        candidates = _rerank_fn(query_text, candidates, top_k=top_k)
    else:
        candidates = candidates[:top_k]

    if log:
        try:
            from pkb.search_log import log_search

            log_search(
                query=query_text,
                category=category,
                top_k=top_k,
                fusion=fusion,
                reranked=rerank,
                results=candidates,
            )
        except Exception:
            pass  # 로깅 실패는 검색을 막지 않음

    return candidates


def _native_search(
    es: Elasticsearch,
    query_text: str,
    query_vector: list[float],
    category: str | None,
    size: int,
) -> list[dict]:
    """기존 방식: ES에 BM25+kNN 동시 전달, 점수 자동 합산."""
    result = es.search(
        index=settings.es_index,
        query=_bm25_query(query_text, category),
        knn=_knn_query(query_vector, size, category),
        size=size,
        source_excludes=["embedding"],
    )
    hits = []
    for hit in result["hits"]["hits"]:
        source = _source_to_dict(hit)
        source["score"] = hit.get("_score", 0.0)
        hits.append(source)
    return hits


def _native_score(bm25_hits: list[dict]) -> list[dict]:
    for h in bm25_hits:
        h["score"] = h.get("_score", 0.0)
    return bm25_hits


def _rrf_search(
    es: Elasticsearch,
    query_text: str,
    query_vector: list[float],
    category: str | None,
    candidate_k: int,
) -> list[dict]:
    """BM25와 kNN을 각각 실행 → Reciprocal Rank Fusion으로 결합."""
    bm25_result = es.search(
        index=settings.es_index,
        query=_bm25_query(query_text, category),
        size=candidate_k,
        source_excludes=["embedding"],
    )
    knn_result = es.search(
        index=settings.es_index,
        knn=_knn_query(query_vector, candidate_k, category),
        size=candidate_k,
        source_excludes=["embedding"],
    )

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
    return results
