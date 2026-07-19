"""청크 provenance 기반 graph edge evidence의 멱등·삭제·재구축 테스트."""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "graph.sqlite")
    init_schema(db)
    connection = get_connection(db)
    yield connection
    connection.close()


def _concepts(conn):
    return (
        gstore.upsert_concept(conn, name="BM25"),
        gstore.upsert_concept(conn, name="RRF"),
    )


def test_same_chunk_evidence_is_idempotent(conn):
    src, dst = _concepts(conn)
    for _ in range(3):
        gstore.add_edge(
            conn,
            src,
            dst,
            "related_to",
            confidence=0.7,
            doc_id="data/rag/x.md",
            chunk_index=0,
        )

    edge = gstore.list_edges(conn, src)[0]
    assert edge["weight"] == 1.0
    assert edge["evidence_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM concept_edge_evidence").fetchone()[0] == 1


def test_multiple_chunks_aggregate_and_clear_exactly(conn):
    src, dst = _concepts(conn)
    gstore.add_edge(
        conn, src, dst, "related_to", confidence=0.5,
        doc_id="data/rag/x.md", chunk_index=0,
    )
    gstore.add_edge(
        conn, src, dst, "related_to", confidence=0.9,
        doc_id="data/rag/y.md", chunk_index=2,
    )
    edge = gstore.list_edges(conn, src)[0]
    assert edge["evidence_count"] == 2
    assert edge["confidence"] == 0.9

    assert gstore.clear_edge_evidence_for_chunk(conn, "data/rag/y.md", 2) == 1
    edge = gstore.list_edges(conn, src)[0]
    assert edge["evidence_count"] == 1
    assert edge["confidence"] == 0.5

    gstore.clear_edge_evidence_for_chunk(conn, "data/rag/x.md", 0)
    assert gstore.list_edges(conn, src) == []


def test_purge_document_removes_only_its_edge_evidence(conn):
    src, dst = _concepts(conn)
    for doc_id in ("data/rag/x.md", "data/rag/y.md"):
        gstore.upsert_document(conn, doc_id, doc_id, "rag")
        gstore.add_edge(
            conn, src, dst, "related_to", doc_id=doc_id, chunk_index=0
        )

    gstore.purge_document(conn, "data/rag/x.md")
    edge = gstore.list_edges(conn, src)[0]
    assert edge["evidence_count"] == 1
    evidence_docs = {
        row["doc_id"] for row in conn.execute("SELECT doc_id FROM concept_edge_evidence")
    }
    assert evidence_docs == {"data/rag/y.md"}


def test_prepare_rebuild_preserves_curated_vocabulary(conn):
    src, dst = _concepts(conn)
    gstore.set_curation(conn, "bm25", "real", prose="보존할 산문")
    gstore.add_mention(conn, src, "data/rag/x.md", 0)
    gstore.add_edge(
        conn, src, dst, "related_to", doc_id="data/rag/x.md", chunk_index=0
    )
    gstore.record_extraction(conn, "data/rag/x.md", 0, "hash", "now")

    result = gstore.prepare_edge_evidence_rebuild(conn)
    assert result == {
        "edges_preserved": 1,
        "edge_evidence": 1,
        "mentions_preserved": 1,
        "markers": 1,
    }
    assert gstore.find_concept_by_slug(conn, "bm25") is not None
    assert gstore.get_prose(conn, "bm25") == "보존할 산문"
    assert gstore.stats(conn)["edges"] == 1
    assert gstore.stats(conn)["mentions"] == 1
    assert gstore.edge_evidence_rebuild_active(conn) is True
    assert gstore.extracted_markers(conn) == ({}, set())


def test_finalize_rebuild_atomically_replaces_legacy_edges(conn):
    src, dst = _concepts(conn)
    old_other = gstore.upsert_concept(conn, name="Legacy")
    gstore.add_edge(conn, src, old_other, "legacy")
    gstore.prepare_edge_evidence_rebuild(conn)

    gstore.add_edge(
        conn,
        src,
        dst,
        "new_relation",
        doc_id="data/rag/x.md",
        chunk_index=0,
    )
    assert [row["relation"] for row in conn.execute("SELECT relation FROM concept_edges")] == [
        "legacy"
    ]

    result = gstore.finalize_edge_evidence_rebuild(conn)
    assert result == {"edges_before": 1, "edges_after": 1, "edge_evidence": 1}
    assert [row["relation"] for row in conn.execute("SELECT relation FROM concept_edges")] == [
        "new_relation"
    ]
    assert gstore.edge_evidence_rebuild_active(conn) is False


def test_empty_slug_is_rejected(conn):
    with pytest.raises(ValueError, match="빈 slug"):
        gstore.upsert_concept(conn, name="?!")
    assert conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0


def test_merge_repoints_and_deduplicates_edge_evidence(conn):
    winner = gstore.upsert_concept(conn, name="A2A Protocol")
    loser = gstore.upsert_concept(conn, name="Agent2Agent Protocol")
    other = gstore.upsert_concept(conn, name="MCP")
    gstore.add_edge(
        conn, winner, other, "related_to", confidence=0.5,
        doc_id="data/a.md", chunk_index=0,
    )
    gstore.add_edge(
        conn, loser, other, "related_to", confidence=0.9,
        doc_id="data/b.md", chunk_index=0,
    )

    result = gstore.merge_concepts(
        conn, gstore.make_slug("A2A Protocol"), [gstore.make_slug("Agent2Agent Protocol")]
    )

    assert result["evidence_repointed"] == 1
    edge = gstore.list_edges(conn, winner)[0]
    assert edge["evidence_count"] == 2
    assert edge["confidence"] == 0.9
    assert {
        row["src_id"] for row in conn.execute("SELECT src_id FROM concept_edge_evidence")
    } == {winner}
