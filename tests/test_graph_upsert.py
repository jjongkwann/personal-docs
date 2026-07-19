"""upsert_concept 빈 description 채움 단위 테스트. 실제 ES/DB 서버 없이 tmp SQLite로 검증."""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    connection = get_connection(str(db_path))
    yield connection
    connection.close()


def test_upsert_fills_empty_description_with_embedding(conn):
    cid = gstore.upsert_concept(conn, name="BM25", description="")
    assert gstore.find_concept_by_slug(conn, "bm25")["description"] == ""

    cid2 = gstore.upsert_concept(conn, name="BM25", description="랭킹 함수", embedding=[0.1, 0.2])
    conn.commit()

    assert cid2 == cid
    row = gstore.find_concept_by_slug(conn, "bm25")
    assert row["description"] == "랭킹 함수"
    assert row["embedding"] is not None  # description과 함께 embedding도 채움
    assert row["mention_count"] == 1  # upsert 반복은 카운트를 부풀리지 않음 (멘션에서 유도)


def test_upsert_preserves_nonempty_description(conn):
    cid = gstore.upsert_concept(conn, name="BM25", description="원본 설명")
    cid2 = gstore.upsert_concept(conn, name="BM25", description="다른 설명")
    conn.commit()

    assert cid2 == cid
    row = gstore.find_concept_by_slug(conn, "bm25")
    assert row["description"] == "원본 설명"  # 파괴적 덮어쓰기 없음
    assert row["mention_count"] == 1  # 위와 동일 — 재추출이 카운트를 부풀리지 않는다


def test_recompute_mention_counts_reflects_actual_mentions(conn):
    cid = gstore.upsert_concept(conn, name="BM25")
    gstore.add_mention(conn, cid, "data/rag/x.md", 0)
    gstore.add_mention(conn, cid, "data/rag/x.md", 1)
    gstore.recompute_mention_counts(conn, {cid})
    assert gstore.find_concept_by_slug(conn, "bm25")["mention_count"] == 2

    gstore.clear_mentions_for_chunk(conn, "data/rag/x.md", 1)
    gstore.recompute_mention_counts(conn, {cid})
    assert gstore.find_concept_by_slug(conn, "bm25")["mention_count"] == 1


def test_upsert_can_disable_embedding_match(conn, monkeypatch):
    first = gstore.upsert_concept(
        conn, name="Data Parallelism", description="GPU 요청 분산", embedding=[1.0, 0.0]
    )
    monkeypatch.setattr(
        gstore,
        "find_concept_by_embedding",
        lambda *args, **kwargs: (
            conn.execute(
                "SELECT * FROM concepts WHERE id = ?", (first,)
            ).fetchone(),
            0.99,
        ),
    )

    second = gstore.upsert_concept(
        conn,
        name="DP",
        description="동적 계획법",
        embedding=[1.0, 0.0],
        match_by_embedding=False,
    )

    assert second != first
    assert gstore.find_concept_by_slug(conn, "dp")["name"] == "DP"


def test_upsert_can_disable_alias_match(conn):
    original = gstore.upsert_concept(conn, name="Data Parallelism")
    gstore.add_alias(conn, original, "DP")

    separate = gstore.upsert_concept(conn, name="DP", match_by_alias=False)

    assert separate != original
    assert gstore.find_concept_by_slug(conn, "dp")["name"] == "DP"
