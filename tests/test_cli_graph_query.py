"""CLI wrappers for native graph read commands."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pkb.cli import app
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema

runner = CliRunner()


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    monkeypatch.setattr("pkb.cli.settings.graph_db_path", db_path)
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
        gstore.add_edge(conn, source, target, "prerequisite_of", confidence=0.9)
    return db_path


def test_cli_explain_path_and_affected(graph_db):
    explained = runner.invoke(app, ["graph", "explain", "BM25"])
    assert explained.exit_code == 0
    assert json.loads(explained.stdout)["concept"]["slug"] == "bm25"

    path = runner.invoke(app, ["graph", "path", "BM25", "RRF"])
    assert path.exit_code == 0
    assert json.loads(path.stdout)["hops"] == 1

    affected = runner.invoke(
        app,
        ["graph", "affected", "BM25", "--relation", "prerequisite_of"],
    )
    assert affected.exit_code == 0
    assert [node["slug"] for node in json.loads(affected.stdout)["nodes"]] == [
        "bm25",
        "rrf",
    ]


def test_cli_semantic_query(graph_db, monkeypatch):
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[1.0, 0.0]])

    result = runner.invoke(
        app,
        ["graph", "query", "lexical ranking", "--seed-limit", "1", "--depth", "1"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["seeds"][0]["concept"]["slug"] == "bm25"
    assert {node["slug"] for node in payload["nodes"]} == {"bm25", "rrf"}
