"""TBox MCP 도구: 온톨로지 조회, 분류 2단계 흐름, 검증 보고, 추론 질의. ES 없이 tmp SQLite로 검증."""

from __future__ import annotations

import json

import pytest

from pkb import mcp_server
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


def _fn(tool):
    return getattr(tool, "fn", tool)


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    conn = get_connection(db_path)
    ids = {name: gstore.upsert_concept(conn, name=name, kind=kind) for name, kind in (
        ("BM25", None), ("Ranking function", "algorithm"), ("Retrieval algorithm", "algorithm"),
        ("Elasticsearch", None),
    )}
    for name in ids:
        gstore.add_mention(conn, ids[name], "data/rag/a.md", 0, "")
    gstore.add_edge(conn, ids["BM25"], ids["Ranking function"], "is_a", doc_id="data/rag/a.md", chunk_index=0)
    gstore.add_edge(conn, ids["Ranking function"], ids["Retrieval algorithm"], "is_a",
                    doc_id="data/rag/a.md", chunk_index=0)
    gstore.add_edge(conn, ids["Elasticsearch"], ids["BM25"], "implements", doc_id="data/rag/a.md", chunk_index=0)
    gstore.recompute_mention_counts(conn, set(ids.values()))
    conn.commit()
    conn.close()
    return db_path


def test_graph_ontology_returns_the_tbox():
    tbox = json.loads(_fn(mcp_server.graph_ontology)())
    assert tbox["version"] == "pkb-tbox/1"
    relations = {relation["id"]: relation for relation in tbox["relations"]}
    assert relations["part_of"]["transitive"] and relations["part_of"]["acyclic"]
    assert relations["related_to"]["symmetric"] and relations["related_to"]["fallback"]
    assert {kind["id"] for kind in tbox["kinds"]} >= {"algorithm", "software", "metric"}


def test_classify_lists_then_assigns_and_reports_new_violations(graph_db):
    listed = json.loads(_fn(mcp_server.graph_classify)())
    assert {c["slug"] for c in listed["concepts"]} == {"bm25", "elasticsearch"}

    # Elasticsearch implements BM25: software → algorithm은 맞지만 dataset → algorithm은 시그니처 위반
    result = json.loads(_fn(mcp_server.graph_classify)(
        '[{"slug": "bm25", "kind": "algorithm"}, {"slug": "elasticsearch", "kind": "dataset"}]'
    ))
    assert result["updated"] == 2
    assert result["violations"]["count"] == 1

    assert "TBox에 없는 kind" in _fn(mcp_server.graph_classify)('[{"slug": "bm25", "kind": "thing"}]')
    assert "JSON 파싱 실패" in _fn(mcp_server.graph_classify)("[")


def test_graph_validate_reports_hard_and_soft_sections(graph_db):
    report = json.loads(_fn(mcp_server.graph_validate)(sample_limit=5))
    assert report["ok"] is True
    assert report["soft"]["unclassified_concepts"]["count"] == 2
    assert "cycles" in report["hard"]


def test_graph_inferred_returns_premise_paths(graph_db):
    result = json.loads(_fn(mcp_server.graph_inferred)("BM25", relation="is_a"))
    assert [(r["target"]["slug"], r["via"]) for r in result["inferred"]] == [
        ("retrieval algorithm", ["bm25", "ranking function", "retrieval algorithm"])
    ]
    assert result["inferred"][0]["inferred"] is True
    down = json.loads(_fn(mcp_server.graph_inferred)("Retrieval algorithm", relation="is_a", direction="down"))
    assert [r["source"]["slug"] for r in down["inferred"]] == ["bm25"]
    assert "relation must be one of" in _fn(mcp_server.graph_inferred)("BM25", relation="uses")


def test_explain_shows_inferred_ancestors_and_inverse_names(graph_db):
    explained = json.loads(_fn(mcp_server.graph_explain)("BM25"))
    assert [r["target"]["slug"] for r in explained["inferred"]] == ["retrieval algorithm"]
    assert [(e["relation"], e["inverse_relation"]) for e in explained["inbound"]] == [
        ("implements", "implemented_by")
    ]
