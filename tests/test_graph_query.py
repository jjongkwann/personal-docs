"""Native concept-graph explain/path/query/affected read models."""

from __future__ import annotations

import pytest

from pkb.graph import query as graph_query
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    init_schema(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _seed(conn):
    bm25 = gstore.upsert_concept(
        conn,
        name="BM25",
        description="Lexical ranking function",
        category="rag",
        embedding=[1.0, 0.0],
    )
    rrf = gstore.upsert_concept(
        conn,
        name="Reciprocal Rank Fusion",
        description="Rank fusion",
        category="rag",
        embedding=[0.8, 0.2],
        match_by_embedding=False,
    )
    rag = gstore.upsert_concept(
        conn,
        name="Retrieval-Augmented Generation",
        description="Retrieval plus generation",
        category="rag",
        embedding=[0.0, 1.0],
        match_by_embedding=False,
    )
    gstore.add_alias(conn, rrf, "RRF")
    gstore.add_alias(conn, rag, "RAG")
    for doc_id, title in (("data/rag/search.md", "Search"), ("data/rag/rag.md", "RAG")):
        gstore.upsert_document(conn, doc_id, title, "rag")

    gstore.add_mention(conn, bm25, "data/rag/search.md", 0, "Ranking > BM25")
    gstore.add_mention(conn, rrf, "data/rag/search.md", 0, "Ranking > Fusion")
    gstore.add_mention(conn, rrf, "data/rag/rag.md", 1, "RAG > Retrieval")
    gstore.add_mention(conn, rag, "data/rag/rag.md", 1, "RAG > Overview")
    gstore.add_edge(
        conn,
        bm25,
        rrf,
        "prerequisite_of",
        confidence=0.9,
        doc_id="data/rag/search.md",
        chunk_index=0,
    )
    gstore.add_edge(
        conn,
        rrf,
        rag,
        "part_of",
        confidence=0.7,
        doc_id="data/rag/rag.md",
        chunk_index=1,
    )
    conn.commit()
    return bm25, rrf, rag


def test_explain_returns_both_directions_mentions_and_evidence(conn):
    _seed(conn)

    result = graph_query.explain(conn, "RRF")

    assert result["concept"]["slug"] == "reciprocal rank fusion"
    assert result["concept"]["aliases"] == ["RRF"]
    assert [edge["relation"] for edge in result["inbound"]] == ["prerequisite_of"]
    assert [edge["relation"] for edge in result["outbound"]] == ["part_of"]
    inbound = result["inbound"][0]
    assert inbound["confidence_label"] == "explicit"
    assert inbound["evidence"] == [
        {
            "doc_id": "data/rag/search.md",
            "chunk_index": 0,
            "title": "Search",
            "category": "rag",
            "section_path": "Ranking > BM25",
            "confidence": 0.9,
            "confidence_label": "explicit",
        }
    ]
    assert {(m["doc_id"], m["chunk_index"]) for m in result["mentions"]} == {
        ("data/rag/search.md", 0),
        ("data/rag/rag.md", 1),
    }


def test_shortest_path_supports_undirected_directed_and_relation_filter(conn):
    _seed(conn)

    forward = graph_query.shortest_path(conn, "BM25", "RAG", max_hops=3)
    assert forward["found"] is True
    assert forward["hops"] == 2
    assert [node["slug"] for node in forward["nodes"]] == [
        "bm25",
        "reciprocal rank fusion",
        "retrieval-augmented generation",
    ]
    assert [edge["traversal_direction"] for edge in forward["edges"]] == [
        "forward",
        "forward",
    ]

    reverse = graph_query.shortest_path(conn, "RAG", "BM25", max_hops=3)
    assert reverse["found"] is True
    assert [edge["traversal_direction"] for edge in reverse["edges"]] == [
        "reverse",
        "reverse",
    ]

    directed = graph_query.shortest_path(
        conn, "RAG", "BM25", max_hops=3, directed=True
    )
    assert directed["found"] is False
    assert directed["hops"] is None

    filtered = graph_query.shortest_path(
        conn, "BM25", "RAG", max_hops=3, relations=["part_of"]
    )
    assert filtered["found"] is False


def test_query_subgraph_uses_semantic_seed_and_bounded_expansion(conn):
    _seed(conn)

    result = graph_query.query_subgraph(
        conn,
        "lexical scoring",
        query_embedding=[1.0, 0.0],
        seed_limit=1,
        depth=2,
        max_nodes=3,
        min_similarity=0.4,
    )

    assert result["found"] is True
    assert result["seeds"][0]["concept"]["slug"] == "bm25"
    assert result["seeds"][0]["match"] == "semantic"
    assert [(node["slug"], node["depth"]) for node in result["nodes"]] == [
        ("bm25", 0),
        ("reciprocal rank fusion", 1),
        ("retrieval-augmented generation", 2),
    ]
    assert {edge["relation"] for edge in result["edges"]} == {
        "prerequisite_of",
        "part_of",
    }


def test_query_subgraph_has_lexical_alias_fallback_without_embedding(conn):
    _seed(conn)

    result = graph_query.query_subgraph(
        conn,
        "RRF",
        query_embedding=None,
        seed_limit=1,
        depth=0,
    )

    assert result["seeds"][0]["concept"]["slug"] == "reciprocal rank fusion"
    assert result["seeds"][0]["match"] == "exact"
    assert len(result["nodes"]) == 1
    assert result["edges"] == []


def test_query_subgraph_excludes_curated_vocab_from_seeds(conn):
    _seed(conn)
    gstore.upsert_concept(
        conn,
        name="Method",
        embedding=[1.0, 0.0],
        category="rag",
        match_by_embedding=False,
    )
    gstore.set_curation(conn, "method", "vocab")
    conn.commit()

    result = graph_query.query_subgraph(
        conn,
        "Method",
        query_embedding=[1.0, 0.0],
        seed_limit=3,
        depth=0,
        min_similarity=0.4,
    )

    assert all(seed["concept"]["slug"] != "method" for seed in result["seeds"])
    assert all(node["slug"] != "method" for node in result["nodes"])


def test_affected_follows_stored_direction_and_relation_filter(conn):
    _seed(conn)

    result = graph_query.affected(conn, "BM25", max_depth=2)
    assert result["found"] is True
    assert [(node["slug"], node["depth"]) for node in result["nodes"]] == [
        ("bm25", 0),
        ("reciprocal rank fusion", 1),
        ("retrieval-augmented generation", 2),
    ]
    assert all(edge["traversal_direction"] == "forward" for edge in result["edges"])

    filtered = graph_query.affected(
        conn, "BM25", max_depth=2, relations=["part_of"]
    )
    assert filtered["found"] is False
    assert [node["slug"] for node in filtered["nodes"]] == ["bm25"]


def test_missing_concepts_and_bounds_fail_clearly(conn):
    _seed(conn)

    with pytest.raises(ValueError, match="Concept not found"):
        graph_query.explain(conn, "missing")
    with pytest.raises(ValueError, match="Target concept not found"):
        graph_query.shortest_path(conn, "BM25", "missing")
    with pytest.raises(ValueError, match="depth must be between"):
        graph_query.query_subgraph(conn, "BM25", depth=5)
