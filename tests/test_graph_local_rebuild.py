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


@pytest.fixture(autouse=True)
def installed_generation_model(monkeypatch):
    monkeypatch.setattr("pkb.graph.rebuild.generation_model", lambda *a, **k: {"digest": "sha256:test"})


def _chunk():
    return {
        "doc_id": "data/rag/x.md",
        "chunk_index": 0,
        "content_hash": "h",
        "input_hash": "ih",
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
                    ExtractedConcept(name="RRF", kind="algorithm", aliases=[], description="검색 순위 결합")
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
    assert item["content_hash"] == "h"
    assert item["input_hash"] == "ih"
    assert item["concepts"][0]["name"] == "RRF"
    assert item["relations"] == []  # concepts에 없는 관계 끝점은 저장하지 않는다


def test_storage_payload_keeps_relations_with_local_endpoints():
    batch = _batch()
    batch.items[0].concepts.append(
        ExtractedConcept(name="검색", kind="field", aliases=[], description="정보를 찾는 과정")
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
                    ExtractedConcept(name="학습 로드맵", kind="field"),
                    ExtractedConcept(name="RRF", kind="algorithm"),
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
                    ExtractedConcept(name="RRF", kind="algorithm"),
                    ExtractedConcept(name="Data Parallelism", kind="technique", aliases=["DP"]),
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
                "doc_id": "doc_1",
                "chunk_index": 0,
                "concepts": [
                    {"name": f"C{i}", "kind": "technique", "aliases": [], "description": "d"} for i in range(8)
                ],
                "relations": [],
            },
            {
                "doc_id": "doc_1",
                "chunk_index": 0,
                "concepts": [{"name": "C8", "kind": "technique", "aliases": [], "description": "d"}],
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
        "pkb.graph.rebuild.request_json", lambda *a, **k: json.loads(_Resp().read())
    )
    batch, _ = rebuild._ollama_extract(
        [_chunk()], {}, model="gpt-oss:20b", endpoint="http://x", timeout=1
    )
    assert len(batch.items) == 1
    assert len(batch.items[0].concepts) == 9
    assert len(batch.items[0].relations) == 1


@pytest.mark.parametrize("returned", [
    [{"doc_id": "doc_1", "chunk_index": 0}, {"doc_id": "doc_1", "chunk_index": 1}],
    [{"doc_id": "doc_1", "chunk_index": 9}],
    [],
])
def test_ollama_extract_rejects_unexpected_or_omitted_chunk_indices(monkeypatch, returned):
    from pkb.graph import rebuild

    monkeypatch.setattr(rebuild, "request_json", lambda *a, **k: {
        "message": {"content": json.dumps({"items": returned})},
    })
    with pytest.raises(ValueError, match="항목 키 불일치"):
        rebuild._ollama_extract([_chunk()], {}, model="chat:1", endpoint="http://x", timeout=1)


@pytest.mark.parametrize("reference", ["doc_2", "doc1", "DOC_1", "data/rag/x.md"])
def test_ollama_extract_rejects_unknown_document_refs_without_repair(monkeypatch, reference):
    from pkb.graph import rebuild

    monkeypatch.setattr(rebuild, "request_json", lambda *a, **k: {
        "message": {"content": json.dumps({"items": [{"doc_id": reference, "chunk_index": 0}]})},
    })
    with pytest.raises(ValueError, match="알 수 없는 문서 참조"):
        rebuild._ollama_extract([_chunk()], {}, model="chat:1", endpoint="http://x", timeout=1)


def test_ollama_extract_roundtrips_long_unicode_hash_ids_and_only_sends_versioned_fields(monkeypatch):
    from pkb.graph import rebuild

    # Keep even decomposed Unicode and separators byte-for-byte outside the LLM.
    source_id = "data/한글_원본/" + "매우_긴_파일명-" * 20 + "import-" + "a" * 64 + ".md"
    chunk = {**_chunk(), "doc_id": source_id, "input_hash": "private-input-hash", "unversioned": "omit"}
    captured = []

    def request(*args, payload, **kwargs):
        sent = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        captured.extend(sent)
        properties = payload["format"]["$defs"]["ExtractedItem"]["properties"]
        assert properties["doc_id"]["enum"] == ["doc_1"]
        assert properties["chunk_index"]["enum"] == [0]
        assert payload["format"]["properties"]["items"]["minItems"] == 1
        return {"message": {"content": json.dumps({"items": [
            {"doc_id": sent[0]["doc_id"], "chunk_index": sent[0]["chunk_index"]},
        ]})}}

    monkeypatch.setattr(rebuild, "request_json", request)
    batch, _ = rebuild._ollama_extract([chunk], {(source_id, 0): ["RAG"]},
                                      model="chat:1", endpoint="http://x", timeout=1)
    assert batch.items[0].doc_id == source_id
    assert captured[0]["doc_id"] == "doc_1"
    assert captured[0]["legacy_concept_hints"] == ["RAG"]
    assert set(captured[0]) == {
        "doc_id", "chunk_index", "content", "title", "section_path", "category", "legacy_concept_hints",
    }
    assert source_id not in json.dumps(captured, ensure_ascii=False)


def test_ollama_extract_maps_multiple_docs_and_same_doc_chunks_without_collisions(monkeypatch):
    from pkb.graph import rebuild

    chunks = [_chunk(), {**_chunk(), "doc_id": "data/다른_문서.md"}, {**_chunk(), "chunk_index": 1}]

    def request(*args, payload, **kwargs):
        sent = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        assert [(c["doc_id"], c["chunk_index"]) for c in sent] == [("doc_1", 0), ("doc_2", 0), ("doc_1", 1)]
        properties = payload["format"]["$defs"]["ExtractedItem"]["properties"]
        assert properties["doc_id"]["enum"] == ["doc_1", "doc_2"]
        assert properties["chunk_index"]["enum"] == [0, 1]
        assert payload["format"]["properties"]["items"]["minItems"] == 3
        return {"message": {"content": json.dumps({"items": [
            {"doc_id": c["doc_id"], "chunk_index": c["chunk_index"]} for c in reversed(sent)
        ]})}}

    monkeypatch.setattr(rebuild, "request_json", request)
    batch, _ = rebuild._ollama_extract(chunks, {}, model="chat:1", endpoint="http://x", timeout=1)
    assert {(c.doc_id, c.chunk_index) for c in batch.items} == {
        ("data/rag/x.md", 0), ("data/rag/x.md", 1), ("data/다른_문서.md", 0),
    }


def test_ollama_extract_duplicate_items_cannot_hide_an_omitted_chunk(monkeypatch):
    from pkb.graph import rebuild

    monkeypatch.setattr(rebuild, "request_json", lambda *a, **k: {
        "message": {"content": json.dumps({"items": [{"doc_id": "doc_1", "chunk_index": 0}] * 2})},
    })
    with pytest.raises(ValueError, match="항목 키 불일치"):
        rebuild._ollama_extract([_chunk(), {**_chunk(), "chunk_index": 1}], {},
                                model="chat:1", endpoint="http://x", timeout=1)


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
