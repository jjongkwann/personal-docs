"""TBox 정의, 저장 시 관계 검사, 대칭 관계 정규화 이관, 개념 분류 테스트."""

from __future__ import annotations

import json

import pytest

from pkb.graph import ontology, services
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "graph.sqlite")
    monkeypatch.setattr("pkb.config.settings.graph_db_path", path)
    init_schema(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    yield connection
    connection.close()


def _concept(conn, name, kind=None):
    return gstore.upsert_concept(conn, name=name, kind=kind)


def _edge(conn, src, dst, relation, doc_id="data/a.md", chunk_index=0):
    return gstore.add_edge(conn, src, dst, relation, doc_id=doc_id, chunk_index=chunk_index)


# ---------- TBox ----------


def test_relation_characteristics_and_kinds():
    assert {"is_a", "part_of", "prerequisite_of"} == ontology.TRANSITIVE_RELATIONS
    assert ontology.ACYCLIC_RELATIONS == ontology.TRANSITIVE_RELATIONS
    assert {"alternative_to", "related_to"} == ontology.SYMMETRIC_RELATIONS
    assert ontology.RELATIONS["implements"].asymmetric
    assert all(relation.irreflexive for relation in ontology.RELATIONS.values())
    assert "example_of" not in ontology.RELATIONS
    assert ontology.inverse_label("part_of") == "has_part"
    assert ontology.inverse_label("related_to") == "related_to"
    assert len(ontology.KIND_IDS) == 11
    assert {kind.group for kind in ontology.KINDS.values()} == set(ontology.GROUPS)
    described = ontology.describe()
    assert described["version"] == ontology.TBOX_VERSION
    assert json.loads(json.dumps(described)) == described


@pytest.mark.parametrize(
    ("relation", "src", "dst", "ok"),
    [
        ("is_a", "algorithm", "algorithm", True),
        ("is_a", "algorithm", "technique", False),
        ("part_of", "algorithm", "technique", True),
        ("part_of", "software", "field", True),
        ("part_of", "software", "technique", False),
        ("uses", "software", "metric", True),
        ("uses", "field", "software", False),
        ("implements", "software", "algorithm", True),
        ("implements", "dataset", "algorithm", False),
        ("addresses", "technique", "problem", True),
        ("addresses", "technique", "metric", False),
        ("alternative_to", "software", "model", True),
        ("alternative_to", "software", "technique", False),
        ("related_to", "field", "metric", True),
    ],
)
def test_signature(relation, src, dst, ok):
    assert (ontology.check_signature(relation, src, dst) is None) is ok
    assert (relation in ontology.allowed_relations(src, dst)) is ok


def test_unclassified_end_is_not_checked():
    assert ontology.check_signature("implements", None, "problem") is None
    assert ontology.allowed_relations(None, "problem") == list(ontology.RELATION_IDS)


# ---------- 저장 시 검사 ----------


def test_store_rejects_unknown_relation_self_loop_signature_and_cycle(conn):
    a = _concept(conn, "A", "algorithm")
    b = _concept(conn, "B", "algorithm")
    c = _concept(conn, "C", "algorithm")
    problem = _concept(conn, "P", "problem")

    with pytest.raises(gstore.RelationRejectedError) as unknown:
        _edge(conn, a, b, "example_of")
    assert unknown.value.code == "unknown_relation" and "is_a" in unknown.value.reason
    with pytest.raises(gstore.RelationRejectedError) as loop:
        _edge(conn, a, a, "related_to")
    assert loop.value.code == "self_loop"
    with pytest.raises(gstore.RelationRejectedError) as signature:
        _edge(conn, a, problem, "is_a")
    assert signature.value.to_dict()["allowed_relations"] == ["prerequisite_of", "addresses", "related_to"]

    _edge(conn, a, b, "is_a")
    _edge(conn, b, c, "is_a")
    with pytest.raises(gstore.RelationRejectedError) as cycle:
        _edge(conn, c, a, "is_a")
    assert cycle.value.code == "cycle"
    assert cycle.value.cycle == ["c", "a", "b", "c"]
    assert conn.execute("SELECT COUNT(*) FROM concept_edge_evidence").fetchone()[0] == 2
    # 다른 관계라면 같은 방향이라도 순환이 아니다
    _edge(conn, c, a, "prerequisite_of")


def test_symmetric_relation_is_stored_canonically(conn):
    a = _concept(conn, "A")
    b = _concept(conn, "B")
    assert _edge(conn, b, a, "related_to") is False  # 미분류 끝 → 시그니처 미검사
    _edge(conn, a, b, "related_to", doc_id="data/b.md")
    rows = conn.execute("SELECT src_id, dst_id, evidence_count FROM concept_edges").fetchall()
    assert [tuple(row) for row in rows] == [(a, b, 2)]


def _payload(concepts, relations):
    return json.dumps(
        {
            "items": [
                {
                    "doc_id": "data/study/x.md", "chunk_index": 0, "content_hash": "h", "input_hash": "ih",
                    "category": "study", "title": "X", "concepts": concepts, "relations": relations,
                }
            ]
        },
        ensure_ascii=False,
    )


def _json_rows(message, header):
    """header 줄 다음에 이어지는 JSON 행들."""
    lines = message.split(header, 1)[1].splitlines()[1:]
    rows = []
    for line in lines:
        if not line.startswith("{"):
            break
        rows.append(json.loads(line))
    return rows


def test_store_concepts_reports_structured_rejections(db_path, monkeypatch):
    monkeypatch.setattr(services, "_chunk_versions", lambda keys: {key: ("h", "ih") for key in keys})
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: [[0.0, 0.0, 0.0] for _ in texts])
    seeded = get_connection(db_path)
    _concept(seeded, "Legacy")  # 미분류 기존 개념
    _concept(seeded, "BM25", "algorithm")
    seeded.commit()
    seeded.close()

    message = services.store_concepts(_payload(
        [
            {"name": "Elasticsearch", "kind": "software"},
            {"name": "Legacy", "kind": "technique"},
            {"name": "BM25", "kind": "technique"},
            {"name": "Mystery"},
        ],
        [
            {"src": "Elasticsearch", "dst": "BM25", "type": "implements", "confidence": 0.9},
            {"src": "BM25", "dst": "Elasticsearch", "type": "is_a"},
            {"src": "BM25", "dst": "Elasticsearch", "type": "depends_on"},
        ],
    ))

    assert "관계 2개" not in message and "관계 1개" in message
    relations = _json_rows(message, "관계 거부 2건")
    assert [(row["type"], row["code"]) for row in relations] == [
        ("is_a", "signature"),
        ("depends_on", "unknown_relation"),
    ]
    assert relations[0]["allowed_relations"] == ["prerequisite_of", "uses", "related_to"]
    concepts = _json_rows(message, "개념 거부 1건")
    assert concepts[0]["concept"] == "Mystery" and concepts[0]["code"] == "missing_kind"
    warnings = _json_rows(message, "kind 경고 1건")
    assert warnings[0]["slug"] == "bm25" and warnings[0]["stored_kind"] == "algorithm"
    conn = get_connection(db_path)
    kinds = dict(conn.execute("SELECT slug, concept_type FROM concepts"))
    conn.close()
    assert kinds == {"legacy": "technique", "bm25": "algorithm", "elasticsearch": "software"}


# ---------- 이관 ----------


def test_tbox_migration_collapses_reverse_duplicates_and_maps_example_of(db_path):
    conn = get_connection(db_path)
    a, b, c = (_concept(conn, name) for name in ("A", "B", "C"))
    conn.execute("DELETE FROM graph_meta WHERE key = 'tbox_version'")
    evidence = [
        ("data/1.md", 0, b, a, "related_to", 0.5),  # 역방향, 정방향과 같은 청크 → 병합
        ("data/1.md", 0, a, b, "related_to", 0.9),
        ("data/2.md", 0, b, a, "related_to", 0.7),  # 역방향, 다른 청크 → 정방향으로 이동
        ("data/3.md", 0, c, a, "example_of", 0.9),
        ("data/4.md", 0, c, b, "custom_label", None),  # 자유 라벨은 보존
    ]
    conn.executemany("INSERT INTO concept_edge_evidence VALUES (?, ?, ?, ?, ?, ?)", evidence)
    conn.execute(
        "INSERT INTO concept_edges (src_id, dst_id, relation, weight, evidence_count, confidence) "
        "SELECT src_id, dst_id, relation, COUNT(*), COUNT(*), MAX(confidence) FROM concept_edge_evidence "
        "GROUP BY src_id, dst_id, relation"
    )
    conn.commit()
    conn.close()

    init_schema(db_path)

    conn = get_connection(db_path)
    edges = {tuple(row) for row in conn.execute("SELECT src_id, dst_id, relation, evidence_count, confidence "
                                                "FROM concept_edges")}
    evidence_rows = {tuple(row) for row in conn.execute("SELECT * FROM concept_edge_evidence")}
    meta = conn.execute("SELECT value FROM graph_meta WHERE key = 'tbox_version'").fetchone()[0]
    conn.close()
    assert edges == {(a, b, "related_to", 2, 0.9), (c, a, "is_a", 1, 0.9), (c, b, "custom_label", 1, None)}
    assert evidence_rows == {
        ("data/1.md", 0, a, b, "related_to", 0.9),
        ("data/2.md", 0, a, b, "related_to", 0.7),
        ("data/3.md", 0, c, a, "is_a", 0.9),
        ("data/4.md", 0, c, b, "custom_label", None),
    }
    assert meta == ontology.TBOX_VERSION


def test_reverse_rows_written_after_migration_are_fixed_on_next_connection(db_path):
    """TBox 이전 코드로 도는 프로세스가 같은 DB에 역방향 대칭 관계를 써도 다음 연결이 바로잡는다."""
    conn = get_connection(db_path)
    a, b = (_concept(conn, name) for name in ("A", "B"))
    conn.execute("INSERT INTO concept_edge_evidence VALUES ('data/1.md', 0, ?, ?, 'related_to', 0.7)", (b, a))
    conn.execute("INSERT INTO concept_edges VALUES (?, ?, 'related_to', 1.0, 1, 0.7)", (b, a))
    conn.commit()
    conn.close()

    init_schema(db_path)

    conn = get_connection(db_path)
    edges = [tuple(row) for row in conn.execute("SELECT src_id, dst_id, relation FROM concept_edges")]
    conn.close()
    assert edges == [(a, b, "related_to")]


def test_read_connection_does_not_need_the_write_lock(db_path):
    """다른 프로세스가 쓰기 잠금을 쥔 동안에도 읽기 연결은 열린다 — 정규화는 쓰기 연결에서만 한다."""
    import time

    from pkb.graph.visibility import graph_read_connection

    conn = get_connection(db_path)
    a, b = (_concept(conn, name) for name in ("A", "B"))
    conn.execute("INSERT INTO concept_edges VALUES (?, ?, 'related_to', 1.0, 1, 0.7)", (b, a))
    conn.commit()
    writer = get_connection(db_path)
    writer.execute("BEGIN IMMEDIATE")  # 오래 도는 추출기의 쓰기 트랜잭션
    try:
        started = time.monotonic()
        with graph_read_connection(db_path) as reader:
            assert [tuple(row) for row in reader.execute("SELECT src_id, dst_id FROM concept_edges")] == [(b, a)]
        assert time.monotonic() - started < 1
    finally:
        writer.rollback()
        writer.close()
        conn.close()


def test_merge_keeps_symmetric_relations_canonical(conn):
    winner = _concept(conn, "Winner")
    other = _concept(conn, "Other")
    loser = _concept(conn, "Loser")
    _edge(conn, other, loser, "related_to")  # (other, loser) — 병합 뒤 (other, winner)는 역방향이 된다
    gstore.merge_concepts(conn, "winner", ["loser"])
    rows = conn.execute("SELECT src_id, dst_id FROM concept_edge_evidence").fetchall()
    assert [tuple(row) for row in rows] == [(winner, other)]
    assert [tuple(row) for row in conn.execute("SELECT src_id, dst_id FROM concept_edges")] == [(winner, other)]


# ---------- 분류 ----------


def test_list_and_classify_concepts(conn):
    first = _concept(conn, "Popular")
    _concept(conn, "Rare")
    tool = _concept(conn, "Tool", "software")
    gstore.add_alias(conn, first, "Pop")
    for chunk in range(3):
        gstore.add_mention(conn, first, "data/a.md", chunk)
    gstore.recompute_mention_counts(conn, {first})
    _edge(conn, tool, first, "implements")  # Popular가 미분류라 저장됨

    listing = services.list_unclassified_concepts(conn, limit=1)
    assert listing["total"] == 2 and listing["next_offset"] == 1
    assert listing["concepts"][0]["slug"] == "popular" and listing["concepts"][0]["aliases"] == ["Pop"]

    with pytest.raises(ValueError, match="TBox에 없는 kind"):
        services.classify_concepts(conn, [{"slug": "rare", "kind": "algorithm"}, {"slug": "popular", "kind": "x"}])
    assert conn.execute("SELECT COUNT(*) FROM concepts WHERE concept_type IS NULL").fetchone()[0] == 2

    result = services.classify_concepts(
        conn,
        [
            {"slug": "popular", "kind": "problem"},
            {"slug": "tool", "kind": "software"},
            {"slug": "ghost", "kind": "model"},
        ],
    )
    assert result["updated"] == 1 and result["unchanged"] == 1 and result["skipped"] == ["ghost"]
    assert result["changes"] == [{"slug": "popular", "from": None, "to": "problem"}]
    assert result["violations"]["count"] == 1
    assert result["violations"]["samples"][0]["relation"] == "implements"
