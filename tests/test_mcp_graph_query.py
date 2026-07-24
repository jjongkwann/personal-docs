"""MCP wrappers for native graph read tools."""

from __future__ import annotations

import json

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema
from pkb.mcp_server import graph_affected as _graph_affected
from pkb.mcp_server import graph_explain as _graph_explain
from pkb.mcp_server import graph_path as _graph_path
from pkb.mcp_server import graph_query as _graph_query

graph_affected = getattr(_graph_affected, "fn", _graph_affected)
graph_explain = getattr(_graph_explain, "fn", _graph_explain)
graph_path = getattr(_graph_path, "fn", _graph_path)
graph_query = getattr(_graph_query, "fn", _graph_query)


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    init_schema(db_path)
    with get_connection(db_path) as conn:
        source = gstore.upsert_concept(
            conn, name="BM25", embedding=[1.0, 0.0], category="rag"
        )
        target = gstore.upsert_concept(
            conn,
            name="RRF",
            embedding=[0.8, 0.2],
            category="rag",
            match_by_embedding=False,
        )
        gstore.upsert_document(conn, "data/rag/search.md", "Search", "rag")
        gstore.add_mention(conn, source, "data/rag/search.md", 0, "BM25")
        gstore.add_mention(conn, target, "data/rag/search.md", 0, "RRF")
        gstore.add_edge(
            conn,
            source,
            target,
            "prerequisite_of",
            confidence=0.9,
            doc_id="data/rag/search.md",
            chunk_index=0,
        )
    return db_path


def test_graph_explain_and_path_return_json(graph_db):
    explained = json.loads(graph_explain("BM25"))
    assert explained["concept"]["slug"] == "bm25"
    assert explained["outbound"][0]["evidence"][0]["doc_id"] == "data/rag/search.md"

    path = json.loads(graph_path("BM25", "RRF"))
    assert path["found"] is True
    assert path["hops"] == 1


def test_graph_query_uses_local_query_embedding(graph_db, monkeypatch):
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[1.0, 0.0]])

    result = json.loads(graph_query("lexical rank", depth=1, seed_limit=1))

    assert result["found"] is True
    assert result["seeds"][0]["concept"]["slug"] == "bm25"
    assert {node["slug"] for node in result["nodes"]} == {"bm25", "rrf"}


def test_graph_affected_and_guarded_error(graph_db):
    result = json.loads(graph_affected("BM25", relations=["prerequisite_of"]))
    assert result["found"] is True
    assert [node["slug"] for node in result["nodes"]] == ["bm25", "rrf"]

    assert "오류: ValueError" in graph_explain("missing")


def test_graph_query_rejects_empty_text_before_embedding(graph_db, monkeypatch):
    def fail_if_called(_texts):
        raise AssertionError("embedding should not run")

    monkeypatch.setattr("pkb.embeddings.embed", fail_if_called)
    assert "오류: ValueError" in graph_query("   ")
