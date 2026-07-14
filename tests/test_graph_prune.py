"""graph.store.prune_missing_documents / purge_document (R5) 단위 테스트.

실제 ES 없이 tmp SQLite로 dangling mention(코퍼스에서 사라진 문서) 정리 로직만 검증.
"""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "graph.sqlite"
    init_schema(str(db_path))
    connection = get_connection(str(db_path))
    yield connection
    connection.close()


def _seed_two_docs(conn):
    """doc a/b 각각 개념 하나씩 mention."""
    c1 = gstore.upsert_concept(conn, name="개념1")
    c2 = gstore.upsert_concept(conn, name="개념2")
    gstore.upsert_document(conn, doc_id="data/a.md", title="A", category="study")
    gstore.upsert_document(conn, doc_id="data/b.md", title="B", category="study")
    gstore.add_mention(conn, c1, "data/a.md", 0)
    gstore.add_mention(conn, c2, "data/b.md", 0)
    conn.commit()


def test_prune_missing_documents_removes_stale_only(conn):
    _seed_two_docs(conn)
    result = gstore.prune_missing_documents(conn, existing_doc_ids={"data/a.md"})
    assert result == {"mentions_pruned": 1, "documents_pruned": 1}

    remaining_mentions = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM concept_mentions")]
    assert remaining_mentions == ["data/a.md"]
    remaining_docs = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents")]
    assert remaining_docs == ["data/a.md"]


def test_prune_missing_documents_noop_when_all_exist(conn):
    _seed_two_docs(conn)
    result = gstore.prune_missing_documents(conn, existing_doc_ids={"data/a.md", "data/b.md"})
    assert result == {"mentions_pruned": 0, "documents_pruned": 0}


def test_prune_missing_documents_clears_extracted_chunks(conn):
    """stale 문서의 추출 마커도 함께 정리 — 이동 문서는 자동 재추출 대상."""
    _seed_two_docs(conn)
    conn.execute(
        "INSERT INTO extracted_chunks (doc_id, content_hash, extracted_at) VALUES "
        "('data/a.md', 'ha', ''), ('data/b.md', 'hb', '')"
    )
    conn.commit()

    gstore.prune_missing_documents(conn, existing_doc_ids={"data/a.md"})

    remaining = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM extracted_chunks")]
    assert remaining == ["data/a.md"]


def test_purge_document_removes_single_doc(conn):
    _seed_two_docs(conn)
    result = gstore.purge_document(conn, "data/b.md")
    assert result == {"mentions_pruned": 1, "documents_pruned": 1}

    remaining = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM concept_mentions")]
    assert remaining == ["data/a.md"]


def test_purge_document_clears_extracted_chunks(conn):
    """purge_document 경로(pkb delete·watch)도 추출 마커 정리 — 마커가 남으면 삭제 후
    동일 내용 재생성 시 pending에서 빠져 멘션이 영구 결손된다."""
    _seed_two_docs(conn)
    conn.execute(
        "INSERT INTO extracted_chunks (doc_id, content_hash, extracted_at) VALUES "
        "('data/a.md', 'ha', ''), ('data/b.md', 'hb', '')"
    )
    conn.commit()

    gstore.purge_document(conn, "data/b.md")

    remaining = [r["doc_id"] for r in conn.execute("SELECT doc_id FROM extracted_chunks")]
    assert remaining == ["data/a.md"]
