"""Graph provenance survives curated document path moves."""

import pytest

from pkb.graph.schema import graph_connection
from pkb.graph.store import rename_document


def _seed(conn, doc_id: str) -> None:
    conn.execute(
        "INSERT INTO documents (doc_id, title, category) VALUES (?, 'ReAct', 'agent')",
        (doc_id,),
    )
    concept_id = conn.execute(
        "INSERT INTO concepts "
        "(name, slug, base_slug, category, created_at, updated_at) "
        "VALUES ('ReAct', 'react', 'react', 'agent', 'now', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO concept_mentions (concept_id, doc_id, chunk_index) VALUES (?, ?, 0)",
        (concept_id, doc_id),
    )
    conn.execute(
        "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash) VALUES (?, 0, 'hash')",
        (doc_id,),
    )


def test_rename_document_moves_provenance_and_is_idempotent(tmp_path):
    db = tmp_path / "graph.sqlite"
    old = "data/agent/01. 추론과 탐색/ReAct.md"
    new = "data/agent/concepts/reasoning/ReAct.md"
    with graph_connection(str(db)) as conn:
        _seed(conn, old)
        result = rename_document(conn, old, new)
        assert result["documents"] == 1
        assert result["concept_mentions"] == 1
        assert result["extracted_chunks"] == 1
        assert rename_document(conn, old, new) == {
            "documents": 0,
            "concept_mentions": 0,
            "concept_edge_evidence": 0,
            "extracted_chunks": 0,
        }
        for table in ("documents", "concept_mentions", "extracted_chunks"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE doc_id = ?", (new,)
            ).fetchone()[0] == 1


def test_rename_document_rejects_target_collision(tmp_path):
    db = tmp_path / "graph.sqlite"
    with graph_connection(str(db)) as conn:
        _seed(conn, "data/old.md")
        conn.execute(
            "INSERT INTO documents (doc_id, title, category) VALUES ('data/new.md', 'N', 'agent')"
        )
        with pytest.raises(ValueError, match="이미 존재"):
            rename_document(conn, "data/old.md", "data/new.md")
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_id = 'data/old.md'"
        ).fetchone()[0] == 1
