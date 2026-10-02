"""실제 Elasticsearch+nori에 검색 DSL과 msearch 이웃 확장을 검증하는 opt-in 스모크."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch

from pkb.config import settings
from pkb.retrieve import _attach_neighbors, _bm25_query, _rrf_search
from pkb.store import (
    add_chunks,
    create_index,
    get_existing_chunks,
    list_doc_ids,
    list_documents,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PKB_ES_INTEGRATION") != "1",
        reason="PKB_ES_INTEGRATION=1일 때만 실제 ES 스모크 실행",
    ),
]


def _chunk(doc_id: str, index: int, content: str, embedding: list[float]) -> dict:
    return {
        "content": content,
        "embedding": embedding,
        "source_path": doc_id,
        "doc_id": doc_id,
        "category": "integration",
        "chunk_index": index,
        "section_path": "테스트",
        "title": "통합 테스트",
        "tags": [],
        "date_modified": "2026-07-19T00:00:00+00:00",
        "language": "ko",
        "content_hash": f"{doc_id}-{index}",
    }


def test_real_es_accepts_hybrid_dsl_and_neighbor_msearch(monkeypatch):
    es = Elasticsearch(settings.es_host)
    index_name = f"pkb-integration-{uuid4().hex}"
    monkeypatch.setattr(settings, "es_index", index_name)
    dims = settings.embedding_dims
    bm25_vec = [1.0, *([0.0] * (dims - 1))]
    other_vec = [0.0, 1.0, *([0.0] * (dims - 2))]

    try:
        create_index(es)
        add_chunks(
            es,
            [
                _chunk("data/integration/bm25.md", 0, "BM25 lexical retrieval", bm25_vec),
                _chunk("data/integration/bm25.md", 1, "neighbor context", bm25_vec),
                _chunk("data/integration/other.md", 0, "unrelated document", other_vec),
            ],
        )

        results = _rrf_search(es, "BM25", bm25_vec, "integration", candidate_k=5)
        assert results
        assert results[0]["doc_id"] == "data/integration/bm25.md"

        # Lifecycle/category filters must not turn an unmatched lexical query into match-all.
        assert es.count(index=index_name, query=_bm25_query("absentexacttoken", "integration"))["count"] == 0
        es.update(index=index_name, id="data/integration/other.md_0",
                  doc={"aliases": ["metadata-only-alias"]}, refresh=True)
        lexical = es.search(index=index_name, query=_bm25_query("metadata-only-alias", "integration"))
        assert [hit["_source"]["doc_id"] for hit in lexical["hits"]["hits"]] == ["data/integration/other.md"]
        lexical_only = _rrf_search(es, "metadata-only-alias", bm25_vec, "integration",
                                   candidate_k=5, fusion="linear", lexical_weight=1.0)
        assert [hit["doc_id"] for hit in lexical_only] == ["data/integration/other.md"]

        hits = [{"doc_id": "data/integration/bm25.md", "chunk_index": 0}]
        expanded = _attach_neighbors(es, hits, window=1)
        assert [row["chunk_index"] for row in expanded[0]["neighbors"]] == [1]

        assert list_doc_ids(es, "data/integration/") == {
            "data/integration/bm25.md",
            "data/integration/other.md",
        }
        docs = list_documents(es, category="integration")
        assert {doc["doc_id"] for doc in docs} == {
            "data/integration/bm25.md",
            "data/integration/other.md",
        }
        assert len(get_existing_chunks(es, "data/integration/bm25.md")) == 2
    finally:
        es.indices.delete(index=index_name, ignore_unavailable=True)
        es.close()
