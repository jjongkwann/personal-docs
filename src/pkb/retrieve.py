from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.embeddings import embed


def hybrid_search(
    es: Elasticsearch,
    query_text: str,
    category: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """BM25 + kNN 하이브리드 검색. ES가 두 점수를 자동 결합."""
    query_vector = embed([query_text])[0]

    # BM25 쿼리
    bm25_query: dict = {
        "bool": {
            "should": [
                {"match": {"content": {"query": query_text, "boost": 1.0}}},
                {"match": {"title": {"query": query_text, "boost": 0.5}}},
            ],
        }
    }

    if category:
        bm25_query["bool"]["filter"] = [{"term": {"category": category}}]

    # kNN 설정
    knn: dict = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": top_k,
        "num_candidates": top_k * 10,
    }

    if category:
        knn["filter"] = [{"term": {"category": category}}]

    result = es.search(
        index=settings.es_index,
        query=bm25_query,
        knn=knn,
        size=top_k,
        source_excludes=["embedding"],
    )

    hits = []
    for hit in result["hits"]["hits"]:
        source = hit["_source"]
        source["score"] = hit.get("_score", 0.0)
        hits.append(source)

    return hits
