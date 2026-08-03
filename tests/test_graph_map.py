"""Evidence Map (graph map) — view model·HTML 스냅샷 완료 기준 검증."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pkb.cli import app
from pkb.graph import store as gstore
from pkb.graph import viewmap
from pkb.graph.schema import get_connection, init_schema

runner = CliRunner()


@pytest.fixture
def graph_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", db_path)
    monkeypatch.setattr("pkb.cli.settings.graph_db_path", db_path)
    init_schema(db_path)
    with get_connection(db_path) as conn:
        bm25 = gstore.upsert_concept(conn, name="BM25", embedding=[1.0, 0.0], category="rag")
        rrf = gstore.upsert_concept(
            conn, name="RRF", embedding=[0.8, 0.2], category="rag", match_by_embedding=False
        )
        rag = gstore.upsert_concept(
            conn, name="RAG", embedding=[0.0, 1.0], category="arch", match_by_embedding=False
        )
        gstore.upsert_concept(
            conn, name="Orphan", embedding=[0.0, -1.0], match_by_embedding=False
        )
        gstore.upsert_document(conn, "data/rag/search.md", "Search Notes", "rag")
        gstore.add_edge(
            conn, bm25, rrf, "prerequisite_of", confidence=0.9,
            doc_id="data/rag/search.md", chunk_index=3,
        )
        gstore.add_mention(conn, bm25, "data/rag/search.md", 3, "Ranking > Fusion")
        gstore.add_edge(conn, rrf, rag, "part_of", confidence=0.7)
        conn.commit()
    return db_path


def _build(db_path, **kwargs):
    with get_connection(db_path) as conn:
        return viewmap.build(conn, **kwargs)


def test_concept_mode_reaches_evidence(graph_db):
    model = _build(graph_db, concept="BM25", depth=1)
    assert model["mode"] == "concept"
    assert {n["slug"] for n in model["nodes"]} == {"bm25", "rrf"}
    [seed] = [n for n in model["nodes"] if n["role"] == "seed"]
    assert seed["slug"] == "bm25"
    assert seed["mentions"][0]["section_path"] == "Ranking > Fusion"
    [edge] = model["edges"]
    assert edge["confidence_label"] == "explicit"
    assert edge["evidence"][0] == {
        "doc_id": "data/rag/search.md",
        "chunk_index": 3,
        "title": "Search Notes",
        "section_path": "Ranking > Fusion",
        "confidence_label": "explicit",
    }


def test_path_mode_marks_endpoints(graph_db):
    model = _build(graph_db, path=("BM25", "RAG"))
    assert model["found"] is True
    assert [n["slug"] for n in model["nodes"]] == ["bm25", "rrf", "rag"]
    assert [n["role"] for n in model["nodes"]] == ["seed", "path", "seed"]
    assert len(model["edges"]) == 2


def test_path_not_found_still_renders_guidance(graph_db):
    model = _build(graph_db, path=("BM25", "Orphan"))
    assert model["found"] is False
    assert "경로가 없습니다" in model["message"]
    assert {n["slug"] for n in model["nodes"]} == {"bm25", "orphan"}
    assert model["edges"] == []
    assert "경로가 없습니다" in viewmap.render(model)


def test_orphan_concept_guidance(graph_db):
    model = _build(graph_db, concept="Orphan")
    assert "고아 개념" in model["message"]
    assert model["edges"] == []


def test_unknown_concept_raises(graph_db):
    with pytest.raises(ValueError, match="Concept not found"):
        _build(graph_db, concept="없는개념")


def test_exactly_one_mode_required(graph_db):
    with pytest.raises(ValueError):
        _build(graph_db, concept="BM25", query="also this")


def test_html_offline_deterministic_no_private_data(graph_db, tmp_path):
    model = _build(graph_db, concept="BM25", depth=2)
    html = viewmap.render(model)
    again = viewmap.render(_build(graph_db, concept="BM25", depth=2))
    assert html == again  # 같은 입력 → 결정적 출력
    # SVG 네임스페이스 식별자(요청 아님) 외에 외부 URL이 없어야 오프라인 보장
    stripped = html.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in stripped and "https://" not in stripped
    assert str(tmp_path) not in html  # 사용자 절대 경로 미포함
    assert "src=" not in html and "@import" not in html and "fetch(" not in html
    assert "data/rag/search.md" in html  # evidence 위치는 상대 doc_id로 도달 가능


def test_cli_concept_and_query_modes(graph_db, tmp_path, monkeypatch):
    out = tmp_path / "map.html"
    result = runner.invoke(app, ["graph", "map", "--concept", "BM25", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "nodes=2" in result.stdout
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")

    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[1.0, 0.0]])
    result = runner.invoke(app, ["graph", "map", "--query", "lexical ranking", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Evidence Map 생성" in result.stdout


def test_cli_requires_exactly_one_entry(graph_db):
    assert runner.invoke(app, ["graph", "map"]).exit_code != 0
    both = runner.invoke(app, ["graph", "map", "--concept", "BM25", "--path", "BM25", "RAG"])
    assert both.exit_code != 0


def test_cli_unknown_concept_message(graph_db):
    result = runner.invoke(app, ["graph", "map", "--concept", "없는개념"])
    assert result.exit_code == 1
    assert "오류" in result.output


def test_mcp_graph_map_writes_html_and_returns_path(graph_db):
    """MCP 쪽은 브라우저를 열 수 없으니 경로를 돌려줘야 쓸모가 있다."""
    import json

    from pkb.mcp_server import graph_map

    payload = json.loads(graph_map(concept="BM25"))
    assert payload["nodes"] == 2
    assert Path(payload["path"]).read_text(encoding="utf-8").startswith("<!doctype html>")
    assert Path(payload["path"]).parent == Path(graph_db).parent

    assert "정확히 하나" in graph_map(concept="BM25", query="also")
    assert "정확히 하나" in graph_map()
    assert "오류" in graph_map(concept="없는개념")
