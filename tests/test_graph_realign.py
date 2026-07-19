"""재청킹으로 슬롯이 바뀐 문서의 멘션 위치 보정(realign_doc_chunks) 단위 테스트."""

from __future__ import annotations

import pytest

from pkb.graph.schema import get_connection, init_schema
from pkb.graph.store import realign_doc_chunks

DOC = "data/rag/x.md"


@pytest.fixture
def conn(tmp_path):
    db = str(tmp_path / "g.sqlite")
    init_schema(db)
    c = get_connection(db)
    c.execute(
        "INSERT INTO concepts (id, name, slug, created_at, updated_at) "
        "VALUES (1, 'RAG', 'rag', 'now', 'now'), (2, 'BM25', 'bm25', 'now', 'now')"
    )
    yield c
    c.close()


def mentions(c):
    return sorted(
        (r["concept_id"], r["chunk_index"])
        for r in c.execute("SELECT concept_id, chunk_index FROM concept_mentions")
    )


def markers(c):
    return sorted(
        (r["chunk_index"], r["content_hash"])
        for r in c.execute("SELECT chunk_index, content_hash FROM extracted_chunks")
    )


def evidence(c):
    return sorted(
        (r["src_id"], r["dst_id"], r["chunk_index"])
        for r in c.execute("SELECT src_id, dst_id, chunk_index FROM concept_edge_evidence")
    )


def seed(c, pairs, marks):
    c.executemany(
        "INSERT INTO concept_mentions (concept_id, doc_id, chunk_index, section_path) "
        "VALUES (?, ?, ?, '')",
        [(cid, DOC, idx) for cid, idx in pairs],
    )
    c.executemany(
        "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash, extracted_at) "
        "VALUES (?, ?, ?, 'now')",
        [(DOC, idx, h) for idx, h in marks],
    )


def test_moved_chunk_carries_mentions_to_new_slot(conn):
    # 앞에 청크가 삽입돼 내용이 0→1, 1→2로 밀린 경우
    seed(conn, [(1, 0), (2, 1)], [(0, "aa"), (1, "bb")])
    r = realign_doc_chunks(conn, DOC, {0: "aa", 1: "bb"}, {0: "new", 1: "aa", 2: "bb"})
    assert r == {"moved": 2, "dropped": 0}
    assert mentions(conn) == [(1, 1), (2, 2)]
    assert markers(conn) == [(1, "aa"), (2, "bb")]


def test_vanished_chunk_drops_mentions_and_markers(conn):
    # 청크 1의 내용이 사라짐 — 멘션·마커 삭제(그 자리 새 내용은 해시가 달라 pending)
    seed(conn, [(1, 0), (2, 1)], [(0, "aa"), (1, "bb")])
    r = realign_doc_chunks(conn, DOC, {0: "aa", 1: "bb"}, {0: "aa"})
    assert r == {"moved": 0, "dropped": 1}
    assert mentions(conn) == [(1, 0)]
    assert markers(conn) == [(0, "aa")]
    assert conn.execute("SELECT mention_count FROM concepts WHERE id=2").fetchone()[0] == 0


def test_legacy_hash_only_marker_of_vanished_chunk_is_cleared(conn):
    # 레거시 마커가 남으면 사라진 내용이 '추출 완료'로 보여 재추출을 영구히 막는다
    seed(conn, [(1, 0)], [(0, "aa")])
    conn.execute(
        "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash, extracted_at) "
        "VALUES (?, NULL, 'aa', 'now')",
        (DOC,),
    )
    realign_doc_chunks(conn, DOC, {0: "aa"}, {0: "zz"})
    assert markers(conn) == []


def test_duplicate_destination_does_not_collide(conn):
    # 같은 내용의 청크가 둘 — 0의 멘션이 1로 이동해 1의 멘션과 겹쳐도 PK 충돌 없이 중복 제거
    seed(conn, [(1, 0), (1, 1)], [(0, "aa"), (1, "aa")])
    r = realign_doc_chunks(conn, DOC, {0: "aa", 1: "aa"}, {0: "pre", 1: "aa"})
    assert r["moved"] == 1
    assert mentions(conn) == [(1, 1)]


def test_unchanged_layout_is_a_noop(conn):
    seed(conn, [(1, 0), (2, 1)], [(0, "aa"), (1, "bb")])
    r = realign_doc_chunks(conn, DOC, {0: "aa", 1: "bb"}, {0: "aa", 1: "bb"})
    assert r == {"moved": 0, "dropped": 0}
    assert mentions(conn) == [(1, 0), (2, 1)]


def test_moved_and_vanished_chunks_realign_edge_evidence(conn):
    seed(conn, [(1, 0), (2, 1)], [(0, "aa"), (1, "bb")])
    conn.execute(
        "INSERT INTO concept_edge_evidence "
        "(doc_id, chunk_index, src_id, dst_id, relation) VALUES (?, 0, 1, 2, 'related_to')",
        (DOC,),
    )
    conn.execute(
        "INSERT INTO concept_edges (src_id, dst_id, relation) VALUES (1, 2, 'related_to')"
    )

    realign_doc_chunks(conn, DOC, {0: "aa", 1: "bb"}, {0: "new", 1: "aa"})

    assert evidence(conn) == [(1, 2, 1)]
    edge = conn.execute("SELECT evidence_count FROM concept_edges").fetchone()
    assert edge["evidence_count"] == 1
