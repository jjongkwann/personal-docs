"""mcp_server 그래프 큐레이션/병합 도구 레벨 테스트. ES 없이 tmp SQLite로 검증.

병합 내부 로직은 test_graph_merge.py가 커버 — 여기서는 도구의 파싱·검증·보고만 본다.
"""

from __future__ import annotations

import json

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema

# FastMCP @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
from pkb.mcp_server import graph_curate as _graph_curate
from pkb.mcp_server import graph_list_concepts as _graph_list_concepts
from pkb.mcp_server import graph_merge as _graph_merge
from pkb.mcp_server import graph_store_concepts as _graph_store_concepts

graph_curate = getattr(_graph_curate, "fn", _graph_curate)
graph_list_concepts = getattr(_graph_list_concepts, "fn", _graph_list_concepts)
graph_merge = getattr(_graph_merge, "fn", _graph_merge)
graph_store_concepts = getattr(_graph_store_concepts, "fn", _graph_store_concepts)


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    """tmp SQLite를 그래프 DB로 강제 (.env의 GRAPH_DB_PATH 무시)."""
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    return db_path


def _seed(db_path: str, names: tuple[str, ...] = ("bm25", "rrf")) -> None:
    conn = get_connection(db_path)
    for name in names:
        gstore.upsert_concept(conn, name=name, description="")
    conn.commit()
    conn.close()


# ---------- graph_curate ----------


def test_graph_curate_empty_arg_lists_uncurated(graph_db):
    _seed(graph_db)
    conn = get_connection(graph_db)
    gstore.set_curation(conn, "bm25", "real")
    conn.commit()
    conn.close()

    result = json.loads(graph_curate(""))
    assert [c["slug"] for c in result["uncurated"]] == ["rrf"]


def test_graph_curate_rejects_bad_json(graph_db):
    assert "JSON 파싱 실패" in graph_curate("not-json")


def test_graph_curate_rejects_bad_label_without_partial_save(graph_db):
    _seed(graph_db)
    result = graph_curate(
        json.dumps([{"slug": "bm25", "label": "real"}, {"slug": "rrf", "label": "maybe"}])
    )
    assert "오류" in result
    # 검증 실패 배치는 통째로 거부 — 부분 저장 없음
    conn = get_connection(graph_db)
    count = conn.execute("SELECT COUNT(*) AS c FROM concept_curation").fetchone()["c"]
    conn.close()
    assert count == 0


def test_graph_curate_saves_and_reports_skipped(graph_db):
    _seed(graph_db)
    items = [
        {"slug": "bm25", "label": "real", "prose": "BM25 산문"},
        {"slug": "rrf", "label": "vocab"},
        {"slug": "ghost-slug", "label": "real"},
    ]
    result = graph_curate(json.dumps(items, ensure_ascii=False))
    assert "큐레이션 저장: 2개" in result
    assert "ghost-slug" in result

    conn = get_connection(graph_db)
    assert gstore.get_prose(conn, "bm25") == "BM25 산문"
    row = conn.execute(
        "SELECT label FROM concept_curation WHERE slug = 'rrf'"
    ).fetchone()
    conn.close()
    assert row["label"] == "vocab"


# ---------- graph_list_concepts ----------


def test_graph_list_concepts_filters_category_and_truncates_description(graph_db):
    conn = get_connection(graph_db)
    gstore.upsert_concept(conn, name="bm25", description="설" * 200, category="study")
    gstore.upsert_concept(conn, name="rrf", description="", category="rag")
    conn.commit()
    conn.close()

    result = json.loads(graph_list_concepts(category="study"))
    assert [c["slug"] for c in result["concepts"]] == ["bm25"]
    assert len(result["concepts"][0]["description"]) == 80  # 앞 80자만
    assert result["concepts"][0]["mention_count"] == 1

    all_result = json.loads(graph_list_concepts())
    assert all_result["total"] == 2


# ---------- graph_merge ----------


def test_graph_merge_rejects_bad_json(graph_db):
    assert "JSON 파싱 실패" in graph_merge("bm25", "not-json")


def test_graph_merge_rejects_non_string_list(graph_db):
    assert "오류" in graph_merge("bm25", '{"a": 1}')
    assert "오류" in graph_merge("bm25", "[1, 2]")


def test_graph_merge_missing_winner_returns_error(graph_db):
    _seed(graph_db)
    result = graph_merge("ghost-slug", '["bm25"]')
    assert "오류" in result
    assert "ghost-slug" in result


def test_graph_merge_reports_summary_and_skipped(graph_db):
    _seed(graph_db, names=("bm25", "best match 25"))
    result = graph_merge("bm25", '["best match 25", "ghost-slug"]')
    assert "merged=1" in result
    assert "ghost-slug" in result
    assert "sync_concept_notes" in result


# ---------- graph_store_concepts 미해소 관계 보고 ----------


def test_graph_store_concepts_reports_unresolved_relations(graph_db, monkeypatch):
    monkeypatch.setattr(
        "pkb.embeddings.embed", lambda texts: [[0.0, 0.0, 0.0] for _ in texts]
    )

    # 추출 마커 기록용 ES 조회는 no-op 스텁 (마커는 test_graph_incremental.py가 커버)
    class _NoES:
        def mget(self, **kwargs):
            return {"docs": []}

    monkeypatch.setattr("pkb.store.get_client", lambda: _NoES())
    items = {
        "items": [
            {
                "doc_id": "data/study/x.md",
                "chunk_index": 0,
                "category": "study",
                "title": "X",
                "concepts": [{"name": "BM25", "description": "랭킹 함수"}],
                # dst가 concepts에도 DB에도 없음 → 미해소로 보고돼야 함
                "relations": [{"src": "BM25", "dst": "TF-IDF", "type": "related_to"}],
            }
        ]
    }
    result = graph_store_concepts(json.dumps(items, ensure_ascii=False))
    assert "관계 1건 미해소" in result
    assert "BM25→TF-IDF(related_to)" in result
    assert "재호출" in result
