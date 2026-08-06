"""증분 그래프 추출(extracted_chunks 마커 + pending_only) 단위 테스트. ES는 monkeypatch."""

from __future__ import annotations

import json

import pytest

from pkb.graph.schema import get_connection, init_schema

# MCPServer @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
from pkb.mcp_server import graph_list_chunks as _graph_list_chunks
from pkb.mcp_server import graph_store_concepts as _graph_store_concepts

graph_list_chunks = getattr(_graph_list_chunks, "fn", _graph_list_chunks)
graph_store_concepts = getattr(_graph_store_concepts, "fn", _graph_store_concepts)


class FakeES:
    """도구가 쓰는 count/search/mget만 흉내내는 가짜 ES (필터는 무시 — 테스트 스코프 단일 카테고리)."""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks

    def count(self, index=None, query=None):
        return {"count": len(self.chunks)}

    def search(self, index=None, query=None, size=None, from_=0, sort=None,
               source_excludes=None, source_includes=None, search_after=None):
        rows = sorted(self.chunks, key=lambda c: (c["doc_id"], c["chunk_index"]))
        if search_after:
            rows = [
                c for c in rows
                if [c["doc_id"], c["chunk_index"]] > list(search_after)
            ]
        hits = [
            {"_source": dict(c), "sort": [c["doc_id"], c["chunk_index"]]} for c in rows
        ]
        return {"hits": {"hits": hits[from_: from_ + (size or len(hits))]}}

    def mget(self, index=None, ids=None, source_excludes=None, source_includes=None):
        by_id = {f"{c['doc_id']}_{c['chunk_index']}": c for c in self.chunks}
        return {
            "docs": [{"found": i in by_id, "_source": dict(by_id.get(i, {}))} for i in ids]
        }


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    """tmp SQLite를 그래프 DB로 강제 (.env의 GRAPH_DB_PATH 무시)."""
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    return db_path


@pytest.fixture
def fake_es(monkeypatch):
    es = FakeES(
        [
            {"doc_id": "data/study/x.md", "chunk_index": 0, "content_hash": "h0",
             "category": "study", "title": "X", "section_path": "", "content": "본문0"},
            {"doc_id": "data/study/x.md", "chunk_index": 1, "content_hash": "h1",
             "category": "study", "title": "X", "section_path": "", "content": "본문1"},
        ]
    )
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[0.0, 0.0, 0.0] for _ in texts])
    return es


def _store_both_chunks():
    """chunk0은 개념 추출, chunk1은 개념 없음 마커(concepts: [])로 저장."""
    items = {
        "items": [
            {"doc_id": "data/study/x.md", "chunk_index": 0, "category": "study", "title": "X",
             "concepts": [{"name": "BM25", "description": "랭킹 함수"}]},
            {"doc_id": "data/study/x.md", "chunk_index": 1, "category": "study", "title": "X",
             "concepts": []},
        ]
    }
    return graph_store_concepts(json.dumps(items, ensure_ascii=False))


def _extracted_rows(db_path: str) -> set[tuple[str, str]]:
    conn = get_connection(db_path)
    rows = {
        (r["doc_id"], r["content_hash"])
        for r in conn.execute("SELECT doc_id, content_hash FROM extracted_chunks")
    }
    conn.close()
    return rows


def test_pending_only_new_chunks_all_pending(graph_db, fake_es):
    result = json.loads(graph_list_chunks(category="study", pending_only=True))
    assert result["total"] == 2
    assert result["pending"] == 2
    assert [c["chunk_index"] for c in result["chunks"]] == [0, 1]
    assert result["chunks"][0]["content"] == "본문0"  # pending 청크는 content 포함
    assert "next_offset" not in result  # pending_only는 offset 페이징 없음 — pending==0이 종료 신호


def test_pending_scan_pages_past_the_page_size(graph_db, monkeypatch):
    """스캔 페이지 크기를 넘는 코퍼스에서도 꼬리 청크가 pending에 잡히는지.

    단일 size 상한으로 스캔하던 시절, 코퍼스가 상한을 넘자 꼬리 청크가 조용히
    pending에서 빠져 영원히 재추출되지 않았다 (실측 10,123 > 10,000).
    """
    monkeypatch.setattr("pkb.graph.services.SCAN_PAGE_SIZE", 2)
    es = FakeES(
        [
            {"doc_id": "data/study/x.md", "chunk_index": i, "content_hash": f"h{i}",
             "category": "study", "title": "X", "section_path": "", "content": f"본문{i}"}
            for i in range(5)
        ]
    )
    monkeypatch.setattr("pkb.store.get_client", lambda: es)

    result = json.loads(graph_list_chunks(category="study", pending_only=True, limit=10))
    assert result["pending"] == 5  # 페이지(2) 이후의 꼬리 3개도 포함
    assert [c["chunk_index"] for c in result["chunks"]] == [0, 1, 2, 3, 4]


def test_store_marks_extracted_including_empty_concepts(graph_db, fake_es):
    _store_both_chunks()

    # 개념 있는 청크·개념 없음 마커(concepts: []) 둘 다 처리완료로 기록
    assert _extracted_rows(graph_db) == {
        ("data/study/x.md", "h0"),
        ("data/study/x.md", "h1"),
    }

    result = json.loads(graph_list_chunks(category="study", pending_only=True))
    assert result["pending"] == 0
    assert result["chunks"] == []


def test_changed_chunk_becomes_pending_again(graph_db, fake_es):
    _store_both_chunks()
    fake_es.chunks[0]["content_hash"] = "h0-v2"  # 원본 수정 → content_hash 변경 시뮬레이션

    result = json.loads(graph_list_chunks(category="study", pending_only=True))
    assert result["pending"] == 1
    assert [c["chunk_index"] for c in result["chunks"]] == [0]  # 미변경 chunk1은 제외


def test_legacy_chunk_without_hash_always_pending(graph_db, fake_es):
    del fake_es.chunks[1]["content_hash"]
    _store_both_chunks()  # chunk1은 hash 없음 → 마커 기록 불가

    assert _extracted_rows(graph_db) == {("data/study/x.md", "h0")}
    result = json.loads(graph_list_chunks(category="study", pending_only=True))
    assert result["pending"] == 1
    assert [c["chunk_index"] for c in result["chunks"]] == [1]


def test_legacy_marker_db_migrates_without_forcing_reextraction(tmp_path, monkeypatch):
    """구 스키마((doc_id, content_hash) PK) DB → chunk_index=NULL 레거시 행으로 이관.

    통째로 버리면 이미 구축된 그래프 전량이 pending으로 돌아가 재추출을 강요한다.
    """
    import sqlite3

    from pkb.graph import store as gstore

    db_path = str(tmp_path / "graph.sqlite")
    old = sqlite3.connect(db_path)
    old.execute(
        "CREATE TABLE extracted_chunks (doc_id TEXT, content_hash TEXT, extracted_at TEXT, "
        "PRIMARY KEY (doc_id, content_hash))"
    )
    old.execute(
        "INSERT INTO extracted_chunks VALUES ('data/study/x.md', 'h0', '2026-01-01T00:00:00+00:00')"
    )
    old.commit()
    old.close()

    init_schema(db_path)
    conn = get_connection(db_path)
    try:
        by_idx, legacy = gstore.extracted_markers(conn)
        assert by_idx == {}
        assert legacy == {("data/study/x.md", "h0")}
        # 레거시 해시가 일치하면 추출 완료 — 재추출 강요 없음
        src = {"doc_id": "data/study/x.md", "chunk_index": 0, "content_hash": "h0"}
        assert not gstore.is_pending(src, by_idx, legacy)
        # 내용이 바뀌면 pending → 재추출 시 chunk_index 키 마커로 승격되고 레거시 행은 정리
        assert gstore.is_pending({**src, "content_hash": "h0-v2"}, by_idx, legacy)
        gstore.record_extraction(conn, "data/study/x.md", 0, "h0", "2026-02-01T00:00:00+00:00")
        by_idx, legacy = gstore.extracted_markers(conn)
        assert by_idx == {("data/study/x.md", 0): "h0"}
        assert legacy == set()
    finally:
        conn.close()


def test_moved_chunk_becomes_pending(graph_db, fake_es):
    """청크가 다른 인덱스로 이동(내용 그대로)해도 재추출 대상 — 마커가 (doc_id, chunk_index) 키.

    해시만으로 마킹하면 '추출 완료'로 보여 멘션이 옛 chunk_index를 계속 가리킨다.
    """
    _store_both_chunks()
    # 앞에 문단이 삽입돼 chunk0 내용이 chunk1로 밀린 상황
    fake_es.chunks[0]["content_hash"] = "h-new"
    fake_es.chunks[1]["content_hash"] = "h0"

    result = json.loads(graph_list_chunks(category="study", pending_only=True))
    assert result["pending"] == 2
    assert [c["chunk_index"] for c in result["chunks"]] == [0, 1]


def test_reextraction_replaces_mentions_of_chunk(graph_db, fake_es):
    """개정된 청크에서 사라진 개념의 멘션은 남지 않는다 (append가 아니라 교체)."""
    from pkb.graph import store as gstore

    _store_both_chunks()  # chunk0 → BM25
    fake_es.chunks[0]["content_hash"] = "h0-v2"
    graph_store_concepts(json.dumps({
        "items": [
            {"doc_id": "data/study/x.md", "chunk_index": 0, "category": "study", "title": "X",
             "concepts": [{"name": "RRF", "description": "순위 융합"}]},
        ]
    }, ensure_ascii=False))

    conn = get_connection(graph_db)
    try:
        bm25 = gstore.find_concept_by_slug(conn, "bm25")
        rrf = gstore.find_concept_by_slug(conn, "rrf")
        assert gstore.list_mentions(conn, bm25["id"]) == []  # 사라진 개념 → 멘션 제거
        assert bm25["mention_count"] == 0
        assert len(gstore.list_mentions(conn, rrf["id"])) == 1
        assert rrf["mention_count"] == 1
    finally:
        conn.close()

    assert json.loads(graph_list_chunks(category="study", pending_only=True))["pending"] == 0


def test_reextraction_replaces_edge_evidence_without_inflation(graph_db, fake_es):
    """같은 청크 재호출은 멱등이고, 내용 변경 재추출은 과거 관계를 제거한다."""
    from pkb.graph import store as gstore

    first = {
        "items": [
            {
                "doc_id": "data/study/x.md",
                "chunk_index": 0,
                "category": "study",
                "title": "X",
                "concepts": [{"name": "BM25"}, {"name": "RRF"}],
                "relations": [{"src": "BM25", "dst": "RRF", "type": "related_to"}],
            }
        ]
    }
    graph_store_concepts(json.dumps(first, ensure_ascii=False))
    graph_store_concepts(json.dumps(first, ensure_ascii=False))

    conn = get_connection(graph_db)
    bm25 = gstore.find_concept_by_slug(conn, "bm25")
    assert gstore.list_edges(conn, bm25["id"])[0]["evidence_count"] == 1
    conn.close()

    fake_es.chunks[0]["content_hash"] = "h0-v2"
    second = {
        "items": [
            {
                "doc_id": "data/study/x.md",
                "chunk_index": 0,
                "category": "study",
                "title": "X",
                "concepts": [{"name": "BM25"}, {"name": "Vector Search"}],
                "relations": [
                    {"src": "BM25", "dst": "Vector Search", "type": "prerequisite_of"}
                ],
            }
        ]
    }
    graph_store_concepts(json.dumps(second, ensure_ascii=False))

    conn = get_connection(graph_db)
    try:
        bm25 = gstore.find_concept_by_slug(conn, "bm25")
        edges = gstore.list_edges(conn, bm25["id"])
        assert [(edge["relation"], edge["evidence_count"]) for edge in edges] == [
            ("prerequisite_of", 1)
        ]
        assert conn.execute("SELECT COUNT(*) FROM concept_edge_evidence").fetchone()[0] == 1
    finally:
        conn.close()


def test_partial_recall_preserves_existing_mentions(graph_db, fake_es):
    """내용이 그대로인 청크의 부분 재호출(미해소 관계 패치)은 기존 멘션을 지우지 않는다.

    멘션 교체는 '내용이 바뀐 청크'에만 적용 — 아니면 문서가 안내하는 패치 호출이 데이터를 지운다.
    """
    from pkb.graph import store as gstore

    _store_both_chunks()  # chunk0 → BM25
    graph_store_concepts(json.dumps({  # 같은 청크에 누락 개념만 추가 (해시 동일)
        "items": [
            {"doc_id": "data/study/x.md", "chunk_index": 0, "category": "study", "title": "X",
             "concepts": [{"name": "RRF", "description": "순위 융합"}],
             "relations": [{"src": "RRF", "dst": "BM25", "type": "related_to"}]},
        ]
    }, ensure_ascii=False))

    conn = get_connection(graph_db)
    try:
        bm25 = gstore.find_concept_by_slug(conn, "bm25")
        rrf = gstore.find_concept_by_slug(conn, "rrf")
        assert len(gstore.list_mentions(conn, bm25["id"])) == 1  # 1차 저장분 보존
        assert bm25["mention_count"] == 1
        assert len(gstore.list_mentions(conn, rrf["id"])) == 1
        assert len(gstore.list_edges(conn, rrf["id"], "related_to")) == 1  # 관계 해소 성공
    finally:
        conn.close()


def test_pending_only_loop_with_marking_returns_each_chunk_exactly_once(graph_db, monkeypatch):
    """pending > limit: 페이지 저장(마킹) → 재호출 루프가 전 청크를 정확히 한 번씩 반환하고 종료.

    offset 페이징이었다면 저장이 pending을 앞에서 줄여 매 페이지 offset만큼 건너뛴다.
    """
    es = FakeES(
        [
            {"doc_id": "data/study/x.md", "chunk_index": i, "content_hash": f"h{i}",
             "category": "study", "title": "X", "section_path": "", "content": f"본문{i}"}
            for i in range(5)
        ]
    )
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[0.0, 0.0, 0.0] for _ in texts])

    seen: list[int] = []
    for _ in range(10):  # 무한루프 방어
        result = json.loads(graph_list_chunks(category="study", limit=2, pending_only=True))
        if result["pending"] == 0:
            break
        assert result["chunks"]  # pending > 0인데 빈 페이지면 진행 불가
        seen += [c["chunk_index"] for c in result["chunks"]]
        items = {
            "items": [
                {"doc_id": c["doc_id"], "chunk_index": c["chunk_index"],
                 "category": "study", "title": "X", "concepts": []}
                for c in result["chunks"]
            ]
        }
        graph_store_concepts(json.dumps(items, ensure_ascii=False))
    else:
        pytest.fail("pending이 0으로 수렴하지 않음")

    assert seen == [0, 1, 2, 3, 4]  # 누락도 중복도 없음
