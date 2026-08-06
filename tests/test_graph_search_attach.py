"""검색 결과 개념 부착(mentions_for_chunks/top_concepts_by_embedding + search_knowledge) 테스트.

헬퍼는 in-memory SQLite로, 도구 레벨은 embed/_rrf_search monkeypatch로 ES 없이 검증.
"""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import SCHEMA_SQL, get_connection, init_schema

# MCPServer @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
from pkb.mcp_server import search_knowledge as _search_knowledge

search_knowledge = getattr(_search_knowledge, "fn", _search_knowledge)


@pytest.fixture
def conn():
    connection = get_connection(":memory:")
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


# ---------- mentions_for_chunks ----------


def test_mentions_for_chunks_joins_by_pair(conn):
    a = gstore.upsert_concept(conn, name="BM25")
    b = gstore.upsert_concept(conn, name="RRF")
    gstore.add_mention(conn, a, "data/study/x.md", 0)
    gstore.add_mention(conn, b, "data/study/x.md", 1)
    gstore.add_mention(conn, b, "data/study/y.md", 0)  # 요청 밖 쌍 — 미포함

    result = gstore.mentions_for_chunks(
        conn, [("data/study/x.md", 0), ("data/study/x.md", 1)]
    )
    assert result[("data/study/x.md", 0)] == [{"name": "BM25", "slug": "bm25"}]
    assert result[("data/study/x.md", 1)] == [{"name": "RRF", "slug": "rrf"}]
    assert ("data/study/y.md", 0) not in result


def test_mentions_for_chunks_empty_curation_includes_all(conn):
    a = gstore.upsert_concept(conn, name="BM25")
    b = gstore.upsert_concept(conn, name="RRF")
    gstore.add_mention(conn, a, "data/study/x.md", 0)
    gstore.add_mention(conn, b, "data/study/x.md", 0)

    # 큐레이션 테이블 비어있음 → curated_connected_slugs=None → 전량 포함
    result = gstore.mentions_for_chunks(conn, [("data/study/x.md", 0)])
    assert {c["slug"] for c in result[("data/study/x.md", 0)]} == {"bm25", "rrf"}


def test_mentions_for_chunks_filters_by_curated_connected_slugs(conn):
    a = gstore.upsert_concept(conn, name="BM25")
    b = gstore.upsert_concept(conn, name="RRF")
    orphan = gstore.upsert_concept(conn, name="고아개념")
    gstore.add_edge(conn, a, b, "related_to")
    for cid in (a, b, orphan):
        gstore.add_mention(conn, cid, "data/study/x.md", 0)
    # 큐레이션 존재 → real+엣지 보유(bm25/rrf)만 부착, vocab은 제외
    gstore.set_curation(conn, "bm25", "real")
    gstore.set_curation(conn, "rrf", "real")
    gstore.set_curation(conn, "고아개념", "vocab")

    result = gstore.mentions_for_chunks(conn, [("data/study/x.md", 0)])
    assert {c["slug"] for c in result[("data/study/x.md", 0)]} == {"bm25", "rrf"}


# ---------- top_concepts_by_embedding ----------


def test_top_concepts_by_embedding_sorts_and_cuts_threshold(conn):
    gstore.upsert_concept(conn, name="정확일치", description="d", embedding=[1.0, 0.0])
    gstore.upsert_concept(conn, name="근접", description="d", embedding=[0.8, 0.6])
    gstore.upsert_concept(conn, name="무관", description="d", embedding=[0.0, 1.0])  # 코사인 0.0 — 컷
    gstore.upsert_concept(conn, name="임베딩없음", description="d")

    result = gstore.top_concepts_by_embedding(conn, [1.0, 0.0])
    assert [row["name"] for row, _ in result] == ["정확일치", "근접"]
    assert result[0][1] == pytest.approx(1.0)
    assert result[1][1] == pytest.approx(0.8)


# ---------- search_knowledge 부착 (도구 레벨) ----------


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    """tmp SQLite를 그래프 DB로 강제 (.env의 GRAPH_DB_PATH 무시)."""
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    return db_path


def _seed_bm25(db_path: str) -> None:
    c = get_connection(db_path)
    cid = gstore.upsert_concept(c, name="BM25", description="랭킹 함수", embedding=[1.0, 0.0])
    gstore.add_alias(c, cid, "Best Match 25")
    gstore.add_mention(c, cid, "data/study/x.md", 0)
    c.commit()
    c.close()


def _stub_search(monkeypatch) -> list[list[str]]:
    embed_calls: list[list[str]] = []
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    monkeypatch.setattr("pkb.config.settings.rerank_enabled", False)
    monkeypatch.setattr(
        "pkb.retrieve.embed",
        lambda texts: embed_calls.append(list(texts)) or [[1.0, 0.0] for _ in texts],
    )
    return embed_calls


def test_search_knowledge_attaches_concepts_and_vocab(graph_db, monkeypatch):
    _seed_bm25(graph_db)
    embed_calls = _stub_search(monkeypatch)
    monkeypatch.setattr(
        "pkb.retrieve._rrf_search",
        lambda *a, **k: [
            {
                "_id": "data/study/x.md_0",
                "doc_id": "data/study/x.md",
                "chunk_index": 0,
                "source_path": "study/x.md",
                "category": "study",
                "title": "X",
                "section_path": "",
                "content": "본문",
                "score": 0.5,
            }
        ],
    )

    result = search_knowledge("bm25란?")
    assert result.startswith("코퍼스 개념 어휘: BM25(Best Match 25)")
    assert "관련 개념: BM25" in result
    assert "data/_concepts" not in result
    assert embed_calls == [["bm25란?"]]  # 하이브리드 검색과 그래프 어휘가 한 벡터 공유


def test_search_knowledge_vocab_attached_even_without_results(graph_db, monkeypatch):
    _seed_bm25(graph_db)
    _stub_search(monkeypatch)
    monkeypatch.setattr("pkb.retrieve._rrf_search", lambda *a, **k: [])

    result = search_knowledge("bm25란?")
    assert result.startswith("코퍼스 개념 어휘: BM25(Best Match 25)")
    assert "검색 결과가 없습니다" in result
