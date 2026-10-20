"""개념 그래프를 TBox(graph.ontology)에 비춰 검사하고 JSON 보고서를 만든다. 읽기만 한다.

hard는 TBox가 금지하는 상태(저장 시 검사가 막는 것과 그 이전 레거시 데이터), soft는 품질 신호다.
SQL은 한정자 없는 테이블 이름의 SELECT만 쓴다 — 접근 제어 연결의 temp 뷰 위에서도 그대로 돈다.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter, deque
from collections.abc import Iterable

from pkb.graph import ontology

SAMPLE_LIMIT = 20

Adjacency = dict[int, set[int]]


def reachable(adjacency: Adjacency, start: int, *, skip: tuple[int, int] | None = None) -> dict[int, int | None]:
    """start에서 닿는 노드 → BFS 부모 (start는 None). skip 간선 (a, b)는 따라가지 않는다."""
    parent: dict[int, int | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, ()):
            if neighbor in parent or (node, neighbor) == skip:
                continue
            parent[neighbor] = node
            queue.append(neighbor)
    return parent


def path_to(parent: dict[int, int | None], target: int) -> list[int]:
    """reachable 결과에서 start → target 경로."""
    path = [target]
    while parent[path[-1]] is not None:
        path.append(parent[path[-1]])
    return path[::-1]


def _adjacency(edges: Iterable[tuple[int, int]]) -> Adjacency:
    adjacency: Adjacency = {}
    for src_id, dst_id in edges:
        adjacency.setdefault(src_id, set()).add(dst_id)
    return adjacency


class _Section:
    """count와 크기 제한 samples."""

    def __init__(self, limit: int) -> None:
        self.count = 0
        self.samples: list[dict] = []
        self.limit = limit

    def add(self, sample: dict) -> None:
        self.count += 1
        if len(self.samples) < self.limit:
            self.samples.append(sample)

    def as_dict(self, **extra) -> dict:
        return {"count": self.count, **extra, "samples": self.samples}


def signature_violations(
    conn: sqlite3.Connection,
    *,
    concept_ids: Iterable[int] | None = None,
    sample_limit: int = SAMPLE_LIMIT,
) -> dict:
    """두 끝이 모두 분류된 관계 중 시그니처를 어긴 것. concept_ids를 주면 그 개념에 닿는 관계만."""
    sql = (
        "SELECT e.relation, s.slug AS src, s.concept_type AS src_kind, d.slug AS dst, d.concept_type AS dst_kind "
        "FROM concept_edges e JOIN concepts s ON s.id = e.src_id JOIN concepts d ON d.id = e.dst_id "
        "WHERE s.concept_type IS NOT NULL AND d.concept_type IS NOT NULL"
    )
    params: list[int] = []
    if concept_ids is not None:
        ids = sorted(set(concept_ids))
        if not ids:
            return _Section(sample_limit).as_dict()
        marks = ",".join("?" for _ in ids)
        sql += f" AND (e.src_id IN ({marks}) OR e.dst_id IN ({marks}))"
        params = ids + ids
    section = _Section(sample_limit)
    for row in conn.execute(sql + " ORDER BY s.slug, d.slug, e.relation", params):
        if ontology.get_relation(row["relation"]) is None:
            continue  # 모르는 라벨은 unknown_relations가 따로 센다
        reason = ontology.check_signature(row["relation"], row["src_kind"], row["dst_kind"])
        if reason:
            section.add({**dict(row), "reason": reason})
    return section.as_dict()


def validate_graph(conn: sqlite3.Connection, *, sample_limit: int = SAMPLE_LIMIT) -> dict:
    """그래프 전체를 검사한 보고서. 각 항목은 count와 slug 샘플(최대 sample_limit개)."""
    started = time.perf_counter()
    concepts = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id, slug, concept_type FROM concepts")}
    edges = [tuple(row) for row in conn.execute("SELECT src_id, dst_id, relation FROM concept_edges")]
    evidenced = {
        tuple(row) for row in conn.execute("SELECT DISTINCT src_id, dst_id, relation FROM concept_edge_evidence")
    }
    rebuild = conn.execute("SELECT value FROM graph_meta WHERE key = 'edge_evidence_rebuild'").fetchone()
    recorded_tbox = conn.execute("SELECT value FROM graph_meta WHERE key = 'tbox_version'").fetchone()

    def slug(concept_id: int) -> str:
        return concepts[concept_id][0] if concept_id in concepts else str(concept_id)

    def kind(concept_id: int) -> str | None:
        return concepts[concept_id][1] if concept_id in concepts else None

    def edge_sample(src_id: int, dst_id: int, relation: str, **extra) -> dict:
        return {"src": slug(src_id), "dst": slug(dst_id), "relation": relation, **extra}

    unknown, self_loops, unchecked, without_evidence = (_Section(sample_limit) for _ in range(4))
    unknown_labels: Counter[str] = Counter()
    by_relation: dict[str, set[tuple[int, int]]] = {}
    for src_id, dst_id, relation in edges:
        if (src_id, dst_id, relation) not in evidenced:
            without_evidence.add(edge_sample(src_id, dst_id, relation))
        if ontology.get_relation(relation) is None:
            unknown_labels[relation] += 1
            unknown.add(edge_sample(src_id, dst_id, relation))
            continue
        if src_id == dst_id:
            self_loops.add(edge_sample(src_id, dst_id, relation))
            continue
        by_relation.setdefault(relation, set()).add((src_id, dst_id))
        if kind(src_id) is None or kind(dst_id) is None:
            unchecked.count += 1

    # 대칭 관계가 두 방향으로 저장된 쌍
    symmetric_duplicates = _Section(sample_limit)
    for relation in sorted(ontology.SYMMETRIC_RELATIONS):
        pairs = by_relation.get(relation, set())
        for src_id, dst_id in sorted(pairs):
            if src_id < dst_id and (dst_id, src_id) in pairs:
                symmetric_duplicates.add(edge_sample(src_id, dst_id, relation))

    # 비순환 관계의 순환: 간선 (s, d)는 d에서 s로 돌아올 수 있으면 순환 위에 있다
    cycles = _Section(sample_limit)
    cyclic_edges = 0
    redundant = _Section(sample_limit)
    for relation in ontology.RELATION_IDS:
        spec = ontology.RELATIONS[relation]
        pairs = by_relation.get(relation, set())
        if not (spec.acyclic or spec.transitive) or not pairs:
            continue
        adjacency = _adjacency(pairs)
        covered: set[tuple[int, int]] = set()
        for src_id, dst_id in sorted(pairs):
            parent = reachable(adjacency, dst_id)
            if spec.acyclic and src_id in parent:
                cyclic_edges += 1
                if (src_id, dst_id) not in covered:
                    cycle = [src_id, *path_to(parent, src_id)]
                    covered.update(zip(cycle, cycle[1:], strict=False))
                    cycles.add({"relation": relation, "path": [slug(node) for node in cycle]})
            if spec.transitive:
                # A→C가 A→…→C(같은 관계, 2단계 이상)로도 따라 나오면 중복 단언
                via = reachable(adjacency, src_id, skip=(src_id, dst_id))
                if dst_id in via:
                    redundant.add(
                        edge_sample(src_id, dst_id, relation, via=[slug(node) for node in path_to(via, dst_id)])
                    )

    # SKOS S27: related는 broaderTransitive와 겹치지 않는다 — 위계 경로가 있는 쌍의 대칭 관계
    hierarchy = _adjacency(pair for relation in ontology.HIERARCHY_RELATIONS for pair in by_relation.get(relation, ()))
    related_with_hierarchy = _Section(sample_limit)
    for relation in sorted(ontology.SYMMETRIC_RELATIONS):
        for src_id, dst_id in sorted(by_relation.get(relation, ())):
            for start, target in ((src_id, dst_id), (dst_id, src_id)):
                parent = reachable(hierarchy, start)
                if target in parent:
                    related_with_hierarchy.add(
                        edge_sample(src_id, dst_id, relation, path=[slug(node) for node in path_to(parent, target)])
                    )
                    break

    total_edges = len(edges)
    related_to = len(by_relation.get("related_to", ()))
    kinds = Counter(concept_type for _, concept_type in concepts.values())
    unclassified = kinds.pop(None, 0)
    hard = {
        "unknown_relations": unknown.as_dict(labels=dict(unknown_labels.most_common())),
        "self_loops": self_loops.as_dict(),
        "signature_violations": signature_violations(conn, sample_limit=sample_limit),
        "cycles": cycles.as_dict(edges_on_cycles=cyclic_edges),
        "symmetric_duplicates": symmetric_duplicates.as_dict(),
        "edges_without_evidence": without_evidence.as_dict(),
    }
    return {
        "tbox_version": ontology.TBOX_VERSION,
        "recorded_tbox_version": recorded_tbox[0] if recorded_tbox else None,
        "evidence_rebuild_active": bool(rebuild and rebuild[0] == "active"),
        "ok": not any(section["count"] for section in hard.values()),
        "stats": {"concepts": len(concepts), "edges": total_edges, "by_kind": dict(kinds.most_common())},
        "hard": hard,
        "soft": {
            "unclassified_concepts": {"count": unclassified},
            "unchecked_edges": {"count": unchecked.count},
            "related_with_hierarchy": related_with_hierarchy.as_dict(),
            "redundant_transitive_edges": redundant.as_dict(),
            "related_to_share": {
                "count": related_to,
                "edges": total_edges,
                "share": round(related_to / total_edges, 4) if total_edges else 0.0,
            },
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
