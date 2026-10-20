"""TBox 검증 보고서 테스트 — 규칙을 어긴 그래프를 SQL로 직접 만들어 각 항목을 확인한다."""

from __future__ import annotations

import pytest

from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema
from pkb.graph.validate import validate_graph


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "graph.sqlite")
    init_schema(path)
    connection = get_connection(path)
    yield connection
    connection.close()


def _raw_edge(conn, src, dst, relation, *, evidence=True):
    conn.execute("INSERT INTO concept_edges (src_id, dst_id, relation) VALUES (?, ?, ?)", (src, dst, relation))
    if evidence:
        conn.execute(
            "INSERT INTO concept_edge_evidence (doc_id, chunk_index, src_id, dst_id, relation) "
            "VALUES ('d', 0, ?, ?, ?)",
            (src, dst, relation),
        )


def test_clean_graph_is_ok(conn):
    a = gstore.upsert_concept(conn, name="A", kind="algorithm")
    b = gstore.upsert_concept(conn, name="B", kind="technique")
    gstore.add_edge(conn, a, b, "part_of", doc_id="d", chunk_index=0)
    report = validate_graph(conn)
    assert report["ok"] is True
    assert report["stats"] == {"concepts": 2, "edges": 1, "by_kind": {"algorithm": 1, "technique": 1}}
    assert report["soft"]["unclassified_concepts"]["count"] == 0


def test_report_covers_every_hard_and_soft_check(conn):
    ids = {}
    for name, kind in [("a", "algorithm"), ("b", "algorithm"), ("c", "algorithm"), ("s", "software"),
                       ("x", "technique"), ("y", "technique"), ("z", "technique"), ("u", None)]:
        ids[name] = gstore.upsert_concept(conn, name=name, kind=kind)
    a, b, c, s, x, y, z, u = ids.values()
    _raw_edge(conn, a, b, "depends_on")  # 모르는 라벨
    _raw_edge(conn, a, a, "related_to")  # 자기 루프
    _raw_edge(conn, a, s, "is_a")  # 시그니처 위반
    _raw_edge(conn, b, c, "part_of")  # 순환
    _raw_edge(conn, c, b, "part_of")
    _raw_edge(conn, a, x, "related_to")  # 대칭 관계 양방향 저장
    _raw_edge(conn, x, a, "related_to")
    _raw_edge(conn, x, y, "is_a")
    _raw_edge(conn, y, z, "is_a")
    _raw_edge(conn, x, z, "is_a")  # x→y→z로도 나오는 중복 단언
    _raw_edge(conn, x, z, "related_to")  # 위계 경로가 있는 쌍의 related_to (SKOS S27)
    _raw_edge(conn, a, u, "related_to")  # 미분류 끝 → unchecked
    _raw_edge(conn, s, x, "uses", evidence=False)  # 근거 없는 관계

    report = validate_graph(conn)

    hard, soft = report["hard"], report["soft"]
    assert report["ok"] is False
    assert hard["unknown_relations"]["count"] == 1 and hard["unknown_relations"]["labels"] == {"depends_on": 1}
    assert hard["self_loops"]["samples"] == [{"src": "a", "dst": "a", "relation": "related_to"}]
    assert hard["signature_violations"]["count"] == 1
    assert hard["signature_violations"]["samples"][0]["dst_kind"] == "software"
    assert hard["cycles"]["count"] == 1 and hard["cycles"]["edges_on_cycles"] == 2
    assert hard["cycles"]["samples"] == [{"relation": "part_of", "path": ["b", "c", "b"]}]
    assert hard["symmetric_duplicates"]["samples"] == [{"src": "a", "dst": "x", "relation": "related_to"}]
    assert hard["edges_without_evidence"]["samples"] == [{"src": "s", "dst": "x", "relation": "uses"}]
    assert soft["unclassified_concepts"]["count"] == 1
    assert soft["unchecked_edges"]["count"] == 1
    assert soft["related_with_hierarchy"]["samples"] == [
        {"src": "x", "dst": "z", "relation": "related_to", "path": ["x", "z"]}
    ]
    assert soft["redundant_transitive_edges"]["samples"] == [
        {"src": "x", "dst": "z", "relation": "is_a", "via": ["x", "y", "z"]}
    ]
    assert soft["related_to_share"] == {"count": 4, "edges": 13, "share": round(4 / 13, 4)}

    bounded = validate_graph(conn, sample_limit=0)
    assert bounded["hard"]["cycles"]["samples"] == [] and bounded["hard"]["cycles"]["count"] == 1
