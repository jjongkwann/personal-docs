"""Bounded initial graph snapshots and large-graph read query regressions."""

import json

import pytest

from pkb import mcp_server
from pkb.graph import query, store
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def graph(tmp_path, monkeypatch):
    path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", path)
    monkeypatch.setattr("pkb.config.settings.access_policy", "")
    init_schema(path)
    conn = get_connection(path)
    for name in ("RAG", "Retrieval", "Ranking", "Graph", "Orphan"):
        store.upsert_concept(conn, name, embedding=[1.0, 0.0], match_by_embedding=False)
    ids = {row["name"]: row["id"] for row in store.list_concepts(conn)}
    store.add_edge(conn, ids["RAG"], ids["Retrieval"], "part_of", confidence=0.9)
    store.add_edge(conn, ids["Retrieval"], ids["Ranking"], "related_to", confidence=0.7)
    store.add_edge(conn, ids["Graph"], ids["RAG"], "related_to", confidence=0.7)
    conn.commit()
    yield conn, ids
    conn.close()


def test_overview_is_bounded_deterministic_and_decodable(graph):
    conn, _ = graph
    result = query.overview(conn, limit=3)
    assert result == query.overview(conn, limit=3)
    assert 0 < len(result["nodes"]) <= 3
    assert 0 < len(result["edges"]) <= 6
    assert result["total_concepts"] == 5 and result["total_edges"] == 3
    assert result["shown_nodes"] == len(result["nodes"])
    assert result["shown_edges"] == len(result["edges"])
    slugs = {node["slug"] for node in result["nodes"]}
    assert "orphan" not in slugs
    assert all(node["seed"] is False for node in result["nodes"])
    assert all(edge["source"]["slug"] in slugs and edge["target"]["slug"] in slugs for edge in result["edges"])
    assert all(edge["evidence"] == [] for edge in result["edges"])


def test_overview_never_calls_embedding_and_mcp_core_exposes_it(graph, monkeypatch):
    monkeypatch.setattr("pkb.embeddings.embed_queries", lambda _: pytest.fail("overview must not embed"))
    tool = getattr(mcp_server.graph_overview, "fn", mcp_server.graph_overview)
    result = json.loads(tool(limit=4))
    assert len(result["nodes"]) == 4
    assert "graph_overview" in mcp_server.CORE_TOOLS
    assert tool.pkb_access == "read"


def test_empty_overview_reports_totals_without_inventing_connections(graph):
    conn, _ = graph
    conn.execute("DELETE FROM concept_edges")
    result = query.overview(conn, limit=40)
    assert result["nodes"] == result["edges"] == []
    assert result["total_concepts"] == 5 and result["total_edges"] == 0


@pytest.mark.parametrize("limit", [0, 201])
def test_overview_rejects_unbounded_limits(graph, limit):
    with pytest.raises(ValueError, match="limit"):
        query.overview(graph[0], limit=limit)


@pytest.mark.parametrize("text", ["RAG", "ax"])
def test_lexical_seeding_does_not_load_all_vector_records(graph, text):
    conn, ids = graph
    ax = store.upsert_concept(conn, "AX", embedding=[0.0, 1.0], match_by_embedding=False)
    store.add_edge(conn, ax, ids["Graph"], "related_to")
    traced = []
    conn.set_trace_callback(traced.append)
    result = query.query_subgraph(conn, text, depth=0)
    assert result["seeds"][0]["concept"]["slug"] == text.lower()
    assert not any("SELECT * FROM concepts ORDER BY" in sql for sql in traced)
    assert any("SELECT id, slug AS text" in sql for sql in traced)


def test_semantic_scoring_filters_disconnected_ids_before_top_k(graph):
    conn, ids = graph
    result = store.top_concepts_by_embedding(conn, [1.0, 0.0], k=1, concept_ids={ids["Graph"]})
    assert result[0][0]["id"] == ids["Graph"]
    assert result[0][1] == pytest.approx(1.0)


def test_ax_without_lexical_matches_uses_vector_only_scan(graph):
    conn, _ = graph
    traced = []
    conn.set_trace_callback(traced.append)
    result = query.query_subgraph(conn, "ax", query_embedding=[1.0, 0.0], depth=1, max_nodes=3)
    assert result["found"] and len(result["nodes"]) <= 3
    assert result["seeds"][0]["match"] == "semantic"
    assert any("SELECT id, embedding FROM concepts" in sql for sql in traced)
    assert not any("SELECT * FROM concepts WHERE embedding IS NOT NULL" in sql for sql in traced)


def test_every_overview_slug_resolves_when_names_collide_across_namespaces(graph):
    conn, ids = graph
    agent_ax = store.upsert_concept(conn, "AX", category="agent", match_by_embedding=False)
    career_ax = store.upsert_concept(conn, "AX", category="career", match_by_embedding=False)
    store.add_edge(conn, agent_ax, ids["Graph"], "related_to")
    store.add_edge(conn, career_ax, ids["RAG"], "related_to")
    conn.commit()
    assert store.get_concept(conn, "ax") is None  # Write/name ambiguity protection stays intact.
    result = query.overview(conn, limit=20)
    ax_slugs = {node["slug"] for node in result["nodes"] if node["name"] == "AX"}
    assert ax_slugs == {"ax", "ax--career"}
    tool = getattr(mcp_server.graph_explain, "fn", mcp_server.graph_explain)
    for node in result["nodes"]:
        explained = json.loads(tool(node["slug"], evidence_limit=1))
        assert explained["concept"]["slug"] == node["slug"]
    assert query.concept_subgraph(conn, "ax", depth=1)["nodes"]
    assert query.shortest_path(conn, "ax", "ax--career")["found"]
