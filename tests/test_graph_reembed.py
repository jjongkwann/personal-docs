"""개념 벡터 재계산(graph reembed)과 임베딩 모델 기록·불일치 가드. tmp SQLite로 검증."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pkb.cli import app
from pkb.config import settings
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema

JINA = {
    "embedding_model": "jinaai/jina-embeddings-v5-text-small",
    "embedding_task": "retrieval",
    "embedding_document_prompt": "document",
    "embedding_normalize": True,
}
JINA_IDENTITY = "jinaai/jina-embeddings-v5-text-small task=retrieval prompt=document normalize"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "graph.sqlite")
    init_schema(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _legacy_graph(conn) -> None:
    """모델 기록이 생기기 전 그래프: 벡터는 있지만 graph_meta에 embedding_model이 없다."""
    for name, description in (("BM25", "랭킹 함수"), ("RRF", ""), ("하이브리드 검색", "BM25와 kNN 결합")):
        conn.execute(
            "INSERT INTO concepts (name, slug, base_slug, description, embedding, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', '')",
            (name, gstore.make_slug(name), gstore.make_slug(name), description, gstore._pack_embedding([1.0, 0.0])),
        )
    conn.commit()


def _use_jina(monkeypatch) -> None:
    for field, value in JINA.items():
        monkeypatch.setattr(settings, field, value)


def _embed(calls: list[list[str]]):
    def embed(texts, **_):
        calls.append(list(texts))
        return [[float(len(text)), 0.5] for text in texts]

    return embed


def test_legacy_graph_counts_as_bge_and_blocks_other_models(conn, monkeypatch):
    _legacy_graph(conn)
    assert gstore.graph_embedding_model(conn) == gstore.LEGACY_EMBEDDING_MODEL == settings.embedding_model
    assert gstore.top_concepts_by_embedding(conn, [1.0, 0.0])  # 같은 모델이면 그대로

    _use_jina(monkeypatch)
    with pytest.raises(gstore.EmbeddingModelMismatchError, match="graph reembed"):
        gstore.top_concepts_by_embedding(conn, [1.0, 0.0])
    with pytest.raises(gstore.EmbeddingModelMismatchError):
        gstore.find_concept_by_embedding(conn, [1.0, 0.0])
    with pytest.raises(gstore.EmbeddingModelMismatchError):
        gstore.upsert_concept(conn, name="새 개념", embedding=[1.0, 0.0])
    # 벡터 없이 쓰는 경로는 막지 않는다
    gstore.upsert_concept(conn, name="벡터 없는 개념")


def test_first_vector_records_the_model_of_an_empty_graph(conn, monkeypatch):
    _use_jina(monkeypatch)
    assert gstore.graph_embedding_model(conn) is None

    gstore.upsert_concept(conn, name="BM25", embedding=[1.0, 0.0])

    assert gstore.graph_embedding_model(conn) == JINA_IDENTITY
    assert gstore.find_concept_by_embedding(conn, [1.0, 0.0])[0]["slug"] == "bm25"


def test_reembed_recomputes_every_concept_and_records_identity(conn, monkeypatch):
    _legacy_graph(conn)
    _use_jina(monkeypatch)
    calls: list[list[str]] = []

    result = gstore.reembed_concepts(conn, _embed(calls), JINA_IDENTITY)

    assert result == {"updated": 3, "total": 3, "complete": True}
    # graph_store_concepts와 같은 입력: "이름: 설명", 설명이 없으면 이름만
    assert calls == [["BM25: 랭킹 함수", "RRF", "하이브리드 검색: BM25와 kNN 결합"]]
    row = gstore.find_concept_by_slug(conn, "rrf")
    assert gstore._unpack_embedding(row["embedding"]) == [3.0, 0.5]
    assert gstore.graph_embedding_model(conn) == JINA_IDENTITY
    assert gstore.top_concepts_by_embedding(conn, [3.0, 0.5])[0][0]["slug"] == "rrf"

    assert gstore.reembed_concepts(conn, _embed(calls), JINA_IDENTITY)["updated"] == 0  # 이미 끝남
    assert len(calls) == 1


def test_reembed_resumes_and_blocks_vector_use_until_done(conn, monkeypatch):
    _legacy_graph(conn)
    conn.execute("UPDATE concepts SET id = 30 WHERE id = 3")
    conn.commit()
    _use_jina(monkeypatch)
    calls: list[list[str]] = []
    reported = []
    queries = []
    conn.set_trace_callback(queries.append)

    partial = gstore.reembed_concepts(conn, _embed(calls), JINA_IDENTITY, batch_size=1, max_batches=1,
                                     progress=lambda done, total: reported.append((done, total)))

    assert partial["complete"] is False and partial["updated"] == 1
    progress = json.loads(gstore._meta(conn, gstore.EMBEDDING_REEMBED_KEY))
    assert progress["identity"] == JINA_IDENTITY
    with pytest.raises(gstore.EmbeddingModelMismatchError, match="다시 계산하는 중"):
        gstore.top_concepts_by_embedding(conn, [1.0, 0.0])

    rest = gstore.reembed_concepts(conn, _embed(calls), JINA_IDENTITY, batch_size=1,
                                  progress=lambda done, total: reported.append((done, total)))

    assert rest == {"updated": 2, "total": 3, "complete": True}
    assert calls[1:] == [["RRF"], ["하이브리드 검색: BM25와 kNN 결합"]]
    assert reported == [(1, 3), (2, 3), (3, 3)]
    assert sum(query.startswith("SELECT count(*) FROM concepts WHERE id <=") for query in queries) == 1
    assert gstore._meta(conn, gstore.EMBEDDING_REEMBED_KEY) is None


def test_cli_graph_reembed_uses_db_option_and_exit_code(db_path, monkeypatch, tmp_path):
    with get_connection(db_path) as conn:
        _legacy_graph(conn)
    _use_jina(monkeypatch)
    monkeypatch.setattr("pkb.embeddings.embed_documents", _embed([]))
    monkeypatch.setattr(settings, "graph_db_path", str(tmp_path / "unused.sqlite"))
    runner = CliRunner()

    stopped = runner.invoke(app, ["graph", "reembed", "--db", db_path, "--batch-size", "2", "--max-batches", "1"])
    assert stopped.exit_code == 3, stopped.output

    done = runner.invoke(app, ["graph", "reembed", "--db", db_path])
    assert done.exit_code == 0, done.output
    assert "개념 1개" in done.output
    with get_connection(db_path) as conn:
        assert gstore.graph_embedding_model(conn) == JINA_IDENTITY

    missing = runner.invoke(app, ["graph", "reembed", "--db", str(tmp_path / "none.sqlite")])
    assert missing.exit_code == 1
