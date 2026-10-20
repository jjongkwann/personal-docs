"""실제 Elasticsearch에서 권한 필터가 검색·목록·집계·문서·관련 문서 모두에 걸리는지 보는 opt-in 스모크."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import yaml
from elasticsearch import Elasticsearch

from pkb import access
from pkb.config import settings
from pkb.documents import fetch_document_sources
from pkb.retrieve import hybrid_search, related_documents
from pkb.store import add_chunks, create_doc_index, create_index, list_categories, list_documents, refresh_document_rows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PKB_ES_INTEGRATION") != "1", reason="PKB_ES_INTEGRATION=1일 때만 실제 ES 스모크 실행"
    ),
]

PUBLIC = "data/public/canary.md"
PRIVATE = "data/private/canary.md"


def _chunk(doc_id: str, index: int, content: str, embedding: list[float], category: str) -> dict:
    return {
        "content": content, "embedding": embedding, "source_path": doc_id, "doc_id": doc_id,
        "category": category, "chunk_index": index, "section_path": "테스트", "title": f"{category} canary",
        "tags": [], "date_modified": "2026-10-20T00:00:00+00:00", "language": "ko", "content_hash": f"{doc_id}-{index}",
    }


def test_restricted_principal_never_sees_the_private_document(tmp_path, monkeypatch):
    es = Elasticsearch(settings.es_host, request_timeout=60)  # 이 머신의 ES는 색인 생성에 10초 넘게 걸린다
    index_name = f"pkb-access-integration-{uuid4().hex}"
    monkeypatch.setattr(settings, "es_index", index_name)
    vector = [1.0, *([0.0] * (settings.embedding_dims - 1))]
    near = [0.9, 0.1, *([0.0] * (settings.embedding_dims - 2))]
    monkeypatch.setattr("pkb.retrieve.embed_queries", lambda texts: [vector for _ in texts])
    policy = tmp_path / "access.yml"
    policy.write_text(yaml.safe_dump({
        "version": 1,
        "principals": {"bob": {}},
        "rules": [{"path": "data/public", "read": ["*"]}],
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "access_policy", str(policy))
    bob = access.load_policy().principal("bob")

    try:
        create_index(es)
        create_doc_index(es)
        add_chunks(es, [
            _chunk(PUBLIC, 0, "canary public note", near, "public"),
            # 비공개 문서가 질의와 더 가깝다 — 사후 필터였다면 상위 k를 차지해 결과가 비었을 것이다.
            *[_chunk(PRIVATE, i, f"canary SECRET-{i}", vector, "private") for i in range(30)],
        ])
        refresh_document_rows(es, [PUBLIC, PRIVATE])
        es.indices.refresh(index=f"{index_name}_docs")

        with access.acting_as(bob):
            hits = hybrid_search(es, "canary", top_k=5, candidate_k=5, expand_context=1, log=False)
            assert [hit["doc_id"] for hit in hits] == [PUBLIC]
            assert all("SECRET" not in n["content"] for hit in hits for n in hit["neighbors"])
            assert [doc["doc_id"] for doc in list_documents(es)] == [PUBLIC]
            assert list_categories(es) == {"public": 1}
            assert fetch_document_sources(es, PRIVATE) == []
            assert [row["doc_id"] for row in related_documents(es, PUBLIC)] == []
            with pytest.raises(access.AccessDeniedError):
                related_documents(es, PRIVATE)

        with access.acting_as(access.SYSTEM):
            assert {doc["doc_id"] for doc in list_documents(es)} == {PUBLIC, PRIVATE}
            assert list_categories(es) == {"private": 1, "public": 1}
    finally:
        es.indices.delete(index=f"{index_name}_docs", ignore_unavailable=True)
        es.indices.delete(index=index_name, ignore_unavailable=True)
        es.close()
