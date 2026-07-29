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

    result = rebuild_with_ollama(
        batch_size=2, retries=0, progress=lambda message: None
    )

    # 핵심 보장: 기존 멘션을 덮어쓸 수 있는 빈 추출은 절대 저장하지 않는다.
    assert stored == []
    # 전체 중단이 아니라 보류로 바뀌었다. 보류가 남는 한 finalize는 막힌다.
    assert {d["chunk_index"] for d in result["deferred"]} == {0, 1}
    assert all("전부 빈 추출" in d["error"] for d in result["deferred"])
    assert result["ready_to_finalize"] is False


def test_ollama_extract_merges_split_items_for_same_chunk(monkeypatch):
    """개념이 max_length를 넘어 모델이 같은 키로 항목을 쪼개 반환해도 병합해 통과한다."""
    from pkb.graph import rebuild

    split = {
        "items": [
            {
                "doc_id": "data/rag/x.md",
                "chunk_index": 0,
                "concepts": [
                    {"name": f"C{i}", "aliases": [], "description": "d"} for i in range(8)
                ],
                "relations": [],
            },
            {
                "doc_id": "data/rag/x.md",
                "chunk_index": 0,
                "concepts": [{"name": "C8", "aliases": [], "description": "d"}],
                "relations": [
                    {"src": "C0", "dst": "C8", "type": "related_to", "confidence": 0.7}
                ],
            },
        ]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps(split)}}).encode()

    monkeypatch.setattr(
        "pkb.graph.rebuild.urllib.request.urlopen", lambda *a, **k: _Resp()
    )
    batch, _ = rebuild._ollama_extract(
        [_chunk()], {}, model="gpt-oss:20b", endpoint="http://x", timeout=1
    )
    assert len(batch.items) == 1
    assert len(batch.items[0].concepts) == 9
    assert len(batch.items[0].relations) == 1


def test_ollama_extract_drops_hallucinated_extra_chunk_key(monkeypatch):
    """요청한 키가 모두 있으면 지어낸 여분 키는 버리고 통과한다."""
    from pkb.graph import rebuild

    extra = {
        "items": [
            {
                "doc_id": "data/rag/x.md",
                "chunk_index": 0,
                "concepts": [{"name": "C0", "aliases": [], "description": "d"}],
                "relations": [],
            },
            {
                "doc_id": "data/rag/x.md",
                "chunk_index": 1,
                "concepts": [{"name": "C1", "aliases": [], "description": "d"}],
                "relations": [],
            },
        ]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps(extra)}}).encode()

    monkeypatch.setattr(
        "pkb.graph.rebuild.urllib.request.urlopen", lambda *a, **k: _Resp()
    )
    batch, _ = rebuild._ollama_extract(
        [_chunk()], {}, model="gpt-oss:20b", endpoint="http://x", timeout=1
    )
    assert [(item.doc_id, item.chunk_index) for item in batch.items] == [
        ("data/rag/x.md", 0)
    ]


def test_ollama_extract_still_rejects_foreign_chunk_key(monkeypatch):
    """병합 로직이 있어도 요청하지 않은 청크 키는 계속 거부한다."""
    from pkb.graph import rebuild

    foreign = {
        "items": [
            {
                "doc_id": "data/rag/OTHER.md",
                "chunk_index": 9,
                "concepts": [],
                "relations": [],
            }
        ]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps(foreign)}}).encode()

    monkeypatch.setattr(
        "pkb.graph.rebuild.urllib.request.urlopen", lambda *a, **k: _Resp()
    )
    with pytest.raises(ValueError, match="항목 키 불일치"):
        rebuild._ollama_extract(
            [_chunk()], {}, model="gpt-oss:20b", endpoint="http://x", timeout=1
        )


def test_ollama_extract_repairs_mangled_doc_id(monkeypatch):
    """모델이 긴 doc_id의 밑줄을 흘려도 원래 키로 되돌려 통과한다."""
    from pkb.graph import rebuild

    chunk = {**_chunk(), "doc_id": "data/rag/1.1.01_NLP_과제_스펙트럼과_지식_의존도.md"}
    mangled = {
        "items": [
            {
                # 밑줄 하나 누락 — 실제 gpt-oss:20b 응답에서 관측된 형태
                "doc_id": "data/rag/1.1.01_NLP_과제_스펙트럼과_지식의존도.md",
                "chunk_index": 0,
                "concepts": [{"name": "RAG", "aliases": [], "description": "d"}],
                "relations": [],
            }
        ]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"message": {"content": json.dumps(mangled)}}).encode()

    monkeypatch.setattr("pkb.graph.rebuild.urllib.request.urlopen", lambda *a, **k: _Resp())
    batch, _ = rebuild._ollama_extract(
        [chunk], {}, model="gpt-oss:20b", endpoint="http://x", timeout=1
    )
    assert len(batch.items) == 1
    assert batch.items[0].doc_id == chunk["doc_id"]


def test_rebuild_defers_poison_chunk_and_continues(monkeypatch, tmp_path):
    """청크 하나가 계속 실패해도 중단하지 않고 보류한 뒤 다음 청크를 처리한다."""
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    with graph_connection(db_path) as conn:
        graph_store.prepare_edge_evidence_rebuild(conn)

    poison = {**_chunk(), "doc_id": "data/rag/poison.md"}
    good = _chunk()

    def _load(es, conn, *, limit, query=None):
        # 보류 키가 걸러진 뒤에도 배치가 차도록 러너가 limit을 늘려 요청한다.
        return [poison, good][:limit], 2, 2

    monkeypatch.setattr("pkb.graph.rebuild.get_client", lambda: object())
    monkeypatch.setattr("pkb.graph.rebuild.load_pending_batch", _load)
    monkeypatch.setattr("pkb.graph.rebuild.legacy_concept_hints", lambda *a, **k: {})

    def _extract(chunks, hints, **kwargs):
        if chunks[0]["doc_id"] == "data/rag/poison.md":
            raise ValueError("Ollama 항목 키 불일치: missing=[('data/rag/poison.md', 0)]")
        return _batch(), {"total_duration": 1_000_000_000}

    monkeypatch.setattr("pkb.graph.rebuild._ollama_extract", _extract)
    monkeypatch.setattr("pkb.graph.rebuild.store_concepts", lambda payload: "저장 완료")
    monkeypatch.setattr("pkb.graph.rebuild._append_log", lambda record: None)

    result = rebuild_with_ollama(
        batch_size=1, max_batches=1, retries=0, progress=lambda message: None
    )

    assert result["chunks"] == 1, "보류 후 정상 청크를 이어서 처리해야 한다"
    assert [d["doc_id"] for d in result["deferred"]] == ["data/rag/poison.md"]
    assert result["ready_to_finalize"] is False, "보류가 남으면 finalize를 막아야 한다"


def test_rebuild_raises_on_systemic_failure(monkeypatch, tmp_path):
    """연속 실패는 개별 청크가 아니라 장애이므로 보류하지 않고 중단한다."""
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    with graph_connection(db_path) as conn:
        graph_store.prepare_edge_evidence_rebuild(conn)

    def _load(es, conn, *, limit, query=None):
        chunks = [{**_chunk(), "doc_id": f"data/rag/{i}.md"} for i in range(limit)]
        return chunks, 99, 99

    monkeypatch.setattr("pkb.graph.rebuild.get_client", lambda: object())
    monkeypatch.setattr("pkb.graph.rebuild.load_pending_batch", _load)
    monkeypatch.setattr("pkb.graph.rebuild.legacy_concept_hints", lambda *a, **k: {})
    monkeypatch.setattr(
        "pkb.graph.rebuild._ollama_extract",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Ollama 호출 실패: refused")),
    )
    monkeypatch.setattr("pkb.graph.rebuild.store_concepts", lambda payload: "ok")
    monkeypatch.setattr("pkb.graph.rebuild._append_log", lambda record: None)

    with pytest.raises(RuntimeError, match="Ollama 호출 실패"):
        rebuild_with_ollama(batch_size=1, retries=0, progress=lambda message: None)


def test_corpus_vocabulary_prefers_real_and_ranks_by_mentions(tmp_path):
    """전역 어휘는 mention_count 순이며 큐레이션이 있으면 real만 반환한다."""
    from pkb.graph.services import corpus_vocabulary

    db = str(tmp_path / "g.sqlite")
    init_schema(db)
    with graph_connection(db) as conn:
        for name, slug, mentions in (
            ("BM25", "bm25", 50),
            ("방법", "방법", 900),
            ("HNSW", "hnsw", 10),
        ):
            conn.execute(
                "INSERT INTO concepts (name, slug, mention_count, created_at, updated_at) "
                "VALUES (?, ?, ?, '2026-01-01', '2026-01-01')",
                (name, slug, mentions),
            )
        # 큐레이션 없으면 순수 mention 순 — 쓰레기('방법')가 1위로 올라온다
        assert corpus_vocabulary(conn) == ["방법", "BM25", "HNSW"]

        graph_store.set_curation(conn, "bm25", "real")
        graph_store.set_curation(conn, "hnsw", "real")
        graph_store.set_curation(conn, "방법", "vocab")
        assert corpus_vocabulary(conn) == ["BM25", "HNSW"]
        assert corpus_vocabulary(conn, limit=1) == ["BM25"]


def test_build_system_prompt_appends_vocabulary_only_when_present():
    """어휘가 비면 프롬프트가 그대로여야 캐시·기존 동작이 유지된다."""
    from pkb.graph.rebuild import SYSTEM_PROMPT, build_system_prompt

    assert build_system_prompt([]) == SYSTEM_PROMPT
    built = build_system_prompt(["BM25", "HNSW"])
    assert built.startswith(SYSTEM_PROMPT)
    assert built.rstrip().endswith("HNSW")
    assert "reuse the name verbatim" in built
