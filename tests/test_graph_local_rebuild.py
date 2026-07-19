"""Ollama evidence 재구축 러너의 배치·재개·완료 경로 테스트."""

from __future__ import annotations

import json

import pytest

from pkb.graph import store as graph_store
from pkb.graph.rebuild import (
    ExtractedBatch,
    ExtractedConcept,
    ExtractedItem,
    ExtractedRelation,
    _storage_payload,
    rebuild_with_ollama,
)
from pkb.graph.schema import graph_connection, init_schema


def _chunk():
    return {
        "doc_id": "data/rag/x.md",
        "chunk_index": 0,
        "category": "rag",
        "title": "RRF",
        "section_path": "검색",
        "content": "RRF는 여러 검색 순위를 결합한다.",
    }


def _batch():
    return ExtractedBatch(
        items=[
            ExtractedItem(
                doc_id="data/rag/x.md",
                chunk_index=0,
                concepts=[
                    ExtractedConcept(name="RRF", aliases=[], description="검색 순위 결합")
                ],
                relations=[
                    ExtractedRelation(
                        src="RRF", dst="검색", type="related_to", confidence=0.7
                    )
                ],
            )
        ]
    )


def test_storage_payload_uses_trusted_chunk_metadata():
    payload = json.loads(_storage_payload(_batch(), [_chunk()]))
    item = payload["items"][0]
    assert item["doc_id"] == "data/rag/x.md"
    assert item["category"] == "rag"
    assert item["title"] == "RRF"
    assert item["section_path"] == "검색"
    assert item["concepts"][0]["name"] == "RRF"
    assert item["relations"] == []  # concepts에 없는 관계 끝점은 저장하지 않는다


def test_storage_payload_keeps_relations_with_local_endpoints():
    batch = _batch()
    batch.items[0].concepts.append(
        ExtractedConcept(name="검색", aliases=[], description="정보를 찾는 과정")
    )

    payload = json.loads(_storage_payload(batch, [_chunk()]))

    assert payload["items"][0]["relations"] == [
        {"src": "RRF", "dst": "검색", "type": "related_to", "confidence": 0.7}
    ]


def test_storage_payload_drops_navigation_concepts_and_their_relations():
    batch = ExtractedBatch(
        items=[
            ExtractedItem(
                doc_id="data/rag/x.md",
                chunk_index=0,
                concepts=[
                    ExtractedConcept(name="학습 로드맵"),
                    ExtractedConcept(name="RRF"),
                ],
                relations=[
                    ExtractedRelation(
                        src="RRF", dst="학습 로드맵", type="part_of", confidence=0.9
                    )
                ],
            )
        ]
    )

    payload = json.loads(_storage_payload(batch, [_chunk()]))

    assert [item["name"] for item in payload["items"][0]["concepts"]] == ["RRF"]
    assert payload["items"][0]["relations"] == []


def test_storage_payload_requires_source_grounding_for_concepts():
    batch = ExtractedBatch(
        items=[
            ExtractedItem(
                doc_id="data/rag/x.md",
                chunk_index=0,
                concepts=[
                    ExtractedConcept(name="RRF"),
                    ExtractedConcept(name="Data Parallelism", aliases=["DP"]),
                ],
            )
        ]
    )

    payload = json.loads(_storage_payload(batch, [_chunk()]))

    assert [item["name"] for item in payload["items"][0]["concepts"]] == ["RRF"]


def test_rebuild_processes_batch_then_finalizes(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    with graph_connection(db_path) as conn:
        graph_store.prepare_edge_evidence_rebuild(conn)

    monkeypatch.setattr("pkb.graph.rebuild.get_client", lambda: object())
    pending_pages = [([_chunk()], 1, 1), ([], 0, 1)]
    monkeypatch.setattr(
        "pkb.graph.rebuild.load_pending_batch", lambda *a, **k: pending_pages.pop(0)
    )
    monkeypatch.setattr("pkb.graph.rebuild.legacy_concept_hints", lambda *a, **k: {})
    monkeypatch.setattr(
        "pkb.graph.rebuild._ollama_extract",
        lambda *a, **k: (_batch(), {"total_duration": 1_000_000_000}),
    )
    stored = []
    monkeypatch.setattr(
        "pkb.graph.rebuild.store_concepts", lambda payload: stored.append(payload) or "저장 완료"
    )
    monkeypatch.setattr("pkb.graph.rebuild._append_log", lambda record: None)

    result = rebuild_with_ollama(batch_size=1, progress=lambda message: None)

    assert result["complete"] is True
    assert result["ready_to_finalize"] is True
    assert result["batches"] == 1
    assert result["chunks"] == 1
    assert len(stored) == 1
    with graph_connection(db_path) as conn:
        assert graph_store.edge_evidence_rebuild_active(conn) is True


def test_rebuild_rejects_all_empty_multi_chunk_batch(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    with graph_connection(db_path) as conn:
        graph_store.prepare_edge_evidence_rebuild(conn)

    chunks = [_chunk(), {**_chunk(), "chunk_index": 1}]
    empty = ExtractedBatch(
        items=[
            ExtractedItem(doc_id=chunk["doc_id"], chunk_index=chunk["chunk_index"])
            for chunk in chunks
        ]
    )
    monkeypatch.setattr("pkb.graph.rebuild.get_client", lambda: object())
    monkeypatch.setattr(
        "pkb.graph.rebuild.load_pending_batch", lambda *a, **k: (chunks, 2, 2)
    )
    monkeypatch.setattr("pkb.graph.rebuild.legacy_concept_hints", lambda *a, **k: {})
    monkeypatch.setattr(
        "pkb.graph.rebuild._ollama_extract",
        lambda *a, **k: (empty, {"total_duration": 1}),
    )
    stored = []
    monkeypatch.setattr(
        "pkb.graph.rebuild.store_concepts", lambda payload: stored.append(payload) or "ok"
    )
    monkeypatch.setattr("pkb.graph.rebuild._append_log", lambda record: None)

    with pytest.raises(ValueError, match="전부 빈 추출"):
        rebuild_with_ollama(batch_size=2, retries=0, progress=lambda message: None)

    assert stored == []
