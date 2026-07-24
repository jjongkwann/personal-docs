"""Concept-graph read models and bounded traversal queries.

SQLite remains the source of truth.  This module only assembles bounded JSON-ready
views for CLI/MCP consumers; it does not maintain a second NetworkX graph or cache.
"""

from __future__ import annotations

import sqlite3
from collections import deque

from pkb.graph import store as gstore

_EDGE_SELECT = (
    "SELECT e.*, "
    "src.name AS src_name, src.slug AS src_slug, "
    "dst.name AS dst_name, dst.slug AS dst_slug "
    "FROM concept_edges e "
    "JOIN concepts src ON src.id = e.src_id "
    "JOIN concepts dst ON dst.id = e.dst_id "
)


def _bounded(value: int, *, minimum: int, maximum: int, name: str) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _relations(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values or [] if v.strip()))


def _confidence_label(value: float | None) -> str:
    if value is None:
        return "legacy"
    if value >= 0.9:
        return "explicit"
    if value >= 0.7:
        return "inferred"
    return "weak"


def _concept_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_prose: bool = False,
) -> dict:
    curation = conn.execute(
        "SELECT label, prose FROM concept_curation WHERE slug = ?", (row["slug"],)
    ).fetchone()
    result = {
        "name": row["name"],
        "slug": row["slug"],
        "category": row["category"],
        "description": row["description"] or "",
        "mention_count": row["mention_count"],
        "aliases": gstore.list_aliases(conn, row["id"]),
        "curation": curation["label"] if curation else None,
    }
    if include_prose:
        result["prose"] = (curation["prose"] if curation else None) or ""
    return result


def _concept_by_id(conn: sqlite3.Connection, concept_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if row is None:  # Foreign keys make this unreachable for materialized edges.
        raise ValueError(f"Concept id does not exist: {concept_id}")
    return row


def _edge_key(row: sqlite3.Row) -> tuple[int, int, str]:
    return row["src_id"], row["dst_id"], row["relation"]


def _edge_evidence(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    limit: int,
) -> list[dict]:
    if limit == 0:
        return []
    rows = conn.execute(
        "SELECT ev.doc_id, ev.chunk_index, ev.confidence, "
        "d.title, d.category, "
        "COALESCE(src_m.section_path, dst_m.section_path, '') AS section_path "
        "FROM concept_edge_evidence ev "
        "LEFT JOIN documents d ON d.doc_id = ev.doc_id "
        "LEFT JOIN concept_mentions src_m "
        "  ON src_m.concept_id = ev.src_id AND src_m.doc_id = ev.doc_id "
        " AND src_m.chunk_index = ev.chunk_index "
        "LEFT JOIN concept_mentions dst_m "
        "  ON dst_m.concept_id = ev.dst_id AND dst_m.doc_id = ev.doc_id "
        " AND dst_m.chunk_index = ev.chunk_index "
        "WHERE ev.src_id = ? AND ev.dst_id = ? AND ev.relation = ? "
        "ORDER BY COALESCE(ev.confidence, -1) DESC, ev.doc_id, ev.chunk_index "
        "LIMIT ?",
        (row["src_id"], row["dst_id"], row["relation"], limit),
    ).fetchall()
    return [
        {
            "doc_id": evidence["doc_id"],
            "chunk_index": evidence["chunk_index"],
            "title": evidence["title"] or "",
            "category": evidence["category"],
            "section_path": evidence["section_path"],
            "confidence": evidence["confidence"],
            "confidence_label": _confidence_label(evidence["confidence"]),
        }
        for evidence in rows
    ]


def _edge_dict(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    evidence_limit: int,
    traversed_from: int | None = None,
) -> dict:
    result = {
        "source": {"name": row["src_name"], "slug": row["src_slug"]},
        "target": {"name": row["dst_name"], "slug": row["dst_slug"]},
        "relation": row["relation"],
        "weight": row["weight"],
        "evidence_count": row["evidence_count"],
        "confidence": row["confidence"],
        "confidence_label": _confidence_label(row["confidence"]),
        "evidence": _edge_evidence(conn, row, limit=evidence_limit),
    }
    if traversed_from is not None:
        result["traversal_direction"] = (
            "forward" if traversed_from == row["src_id"] else "reverse"
        )
    return result


def _incident_edges(
    conn: sqlite3.Connection,
    concept_id: int,
    *,
    relations: list[str] | None = None,
    outgoing_only: bool = False,
) -> list[sqlite3.Row]:
    filters = _relations(relations)
    if outgoing_only:
        where = "e.src_id = ?"
        params: list[object] = [concept_id]
    else:
        where = "(e.src_id = ? OR e.dst_id = ?)"
        params = [concept_id, concept_id]
    if filters:
        where += f" AND e.relation IN ({','.join('?' for _ in filters)})"
        params.extend(filters)
    return list(
        conn.execute(
            _EDGE_SELECT
            + f"WHERE {where} "
            + "ORDER BY e.evidence_count DESC, COALESCE(e.confidence, 0) DESC, "
            + "e.relation, src.slug, dst.slug",
            params,
        ).fetchall()
    )


def _mentions(conn: sqlite3.Connection, concept_id: int, *, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT m.doc_id, m.chunk_index, m.section_path, d.title, d.category "
        "FROM concept_mentions m "
        "LEFT JOIN documents d ON d.doc_id = m.doc_id "
        "WHERE m.concept_id = ? "
        "ORDER BY m.doc_id, m.chunk_index LIMIT ?",
        (concept_id, limit),
    ).fetchall()
    return [
        {
            "doc_id": row["doc_id"],
            "chunk_index": row["chunk_index"],
            "title": row["title"] or "",
            "category": row["category"],
            "section_path": row["section_path"] or "",
        }
        for row in rows
    ]


def explain(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    edge_limit: int = 30,
    evidence_limit: int = 5,
    mention_limit: int = 20,
) -> dict:
    """Return one concept with inbound/outbound relationships and provenance."""
    edge_limit = _bounded(edge_limit, minimum=1, maximum=100, name="edge_limit")
    evidence_limit = _bounded(
        evidence_limit, minimum=0, maximum=20, name="evidence_limit"
    )
    mention_limit = _bounded(mention_limit, minimum=0, maximum=100, name="mention_limit")
    row = gstore.get_concept(conn, identifier)
    if row is None:
        raise ValueError(f"Concept not found: {identifier}")

    incident = _incident_edges(conn, row["id"])
    outbound = [edge for edge in incident if edge["src_id"] == row["id"]][:edge_limit]
    inbound = [edge for edge in incident if edge["dst_id"] == row["id"]][:edge_limit]
    return {
        "concept": _concept_dict(conn, row, include_prose=True),
        "outbound": [
            _edge_dict(conn, edge, evidence_limit=evidence_limit) for edge in outbound
        ],
        "inbound": [
            _edge_dict(conn, edge, evidence_limit=evidence_limit) for edge in inbound
        ],
        "mentions": _mentions(conn, row["id"], limit=mention_limit),
    }


def concept_subgraph(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    depth: int = 1,
    max_nodes: int = 30,
    relations: list[str] | None = None,
    evidence_limit: int = 3,
) -> dict:
    """Expand one concept's undirected neighborhood (map/explore views)."""
    depth = _bounded(depth, minimum=0, maximum=4, name="depth")
    max_nodes = _bounded(max_nodes, minimum=1, maximum=100, name="max_nodes")
    evidence_limit = _bounded(
        evidence_limit, minimum=0, maximum=20, name="evidence_limit"
    )
    root = gstore.get_concept(conn, identifier)
    if root is None:
        raise ValueError(f"Concept not found: {identifier}")
    depth_by_id, edges = _expand(
        conn,
        [root["id"]],
        depth=depth,
        max_nodes=max_nodes,
        relations=relations,
        directed=False,
    )
    return {
        "found": True,
        "root": _concept_dict(conn, root),
        "relations": _relations(relations),
        "nodes": [
            {
                **_concept_dict(conn, _concept_by_id(conn, concept_id)),
                "depth": concept_depth,
                "seed": concept_id == root["id"],
            }
            for concept_id, concept_depth in depth_by_id.items()
        ],
        "edges": [
            _edge_dict(conn, edge, evidence_limit=evidence_limit) for edge in edges
        ],
    }


def shortest_path(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    *,
    max_hops: int = 4,
    directed: bool = False,
    relations: list[str] | None = None,
    evidence_limit: int = 3,
) -> dict:
    """Find a bounded shortest path between two concepts."""
    max_hops = _bounded(max_hops, minimum=1, maximum=8, name="max_hops")
    evidence_limit = _bounded(
        evidence_limit, minimum=0, maximum=20, name="evidence_limit"
    )
    source_row = gstore.get_concept(conn, source)
    target_row = gstore.get_concept(conn, target)
    if source_row is None:
        raise ValueError(f"Source concept not found: {source}")
    if target_row is None:
        raise ValueError(f"Target concept not found: {target}")

    source_id, target_id = source_row["id"], target_row["id"]
    parent: dict[int, tuple[int, sqlite3.Row] | None] = {source_id: None}
    depth_by_id = {source_id: 0}
    queue: deque[int] = deque([source_id])
    found = source_id == target_id
    while queue and not found:
        current = queue.popleft()
        depth = depth_by_id[current]
        if depth >= max_hops:
            continue
        for edge in _incident_edges(
            conn, current, relations=relations, outgoing_only=directed
        ):
            neighbor = (
                edge["dst_id"] if edge["src_id"] == current else edge["src_id"]
            )
            if neighbor in parent:
                continue
            parent[neighbor] = (current, edge)
            depth_by_id[neighbor] = depth + 1
            if neighbor == target_id:
                found = True
                break
            queue.append(neighbor)

    base = {
        "found": found,
        "directed": directed,
        "relations": _relations(relations),
        "source": _concept_dict(conn, source_row),
        "target": _concept_dict(conn, target_row),
        "max_hops": max_hops,
    }
    if not found:
        return {**base, "hops": None, "nodes": [], "edges": []}

    node_ids = [target_id]
    path_edges: list[tuple[int, sqlite3.Row]] = []
    cursor = target_id
    while parent[cursor] is not None:
        previous, edge = parent[cursor]
        path_edges.append((previous, edge))
        node_ids.append(previous)
        cursor = previous
    node_ids.reverse()
    path_edges.reverse()
    return {
        **base,
        "hops": len(path_edges),
        "nodes": [_concept_dict(conn, _concept_by_id(conn, node_id)) for node_id in node_ids],
        "edges": [
            _edge_dict(
                conn,
                edge,
                evidence_limit=evidence_limit,
                traversed_from=previous,
            )
            for previous, edge in path_edges
        ],
    }


def _seed_candidates(
    conn: sqlite3.Connection,
    query: str,
    *,
    query_embedding: list[float] | None,
    limit: int,
    min_similarity: float,
) -> list[dict]:
    q = gstore.make_slug(query)
    if not q:
        return []
    terms = q.split()
    candidates: dict[int, dict] = {}
    curation_by_slug = {
        row["slug"]: row["label"]
        for row in conn.execute("SELECT slug, label FROM concept_curation")
    }
    aliases_by_id: dict[int, list[str]] = {}
    for alias in conn.execute(
        "SELECT concept_id, alias FROM concept_aliases ORDER BY concept_id, alias_slug"
    ):
        aliases_by_id.setdefault(alias["concept_id"], []).append(alias["alias"])
    connected_ids = {
        row["concept_id"]
        for row in conn.execute(
            "SELECT src_id AS concept_id FROM concept_edges "
            "UNION SELECT dst_id AS concept_id FROM concept_edges"
        )
    }

    for row in gstore.list_concepts(conn):
        if row["id"] not in connected_ids:
            continue
        if curation_by_slug.get(row["slug"]) == "vocab":
            continue
        texts = [row["slug"], gstore.make_slug(row["name"])]
        texts.extend(
            gstore.make_slug(alias) for alias in aliases_by_id.get(row["id"], [])
        )
        score = 0.0
        match = ""
        if q in texts:
            score, match = 1.0, "exact"
        elif any(text.startswith(q) or q.startswith(text) for text in texts if text):
            score, match = 0.9, "prefix"
        else:
            matched = sum(any(term in text for text in texts) for term in terms)
            if matched:
                score = 0.4 + 0.4 * (matched / len(terms))
                match = "lexical"
        if score:
            candidates[row["id"]] = {"row": row, "score": score, "match": match}

    if query_embedding:
        for row, score in gstore.top_concepts_by_embedding(
            conn, query_embedding, k=max(limit * 3, 10), threshold=min_similarity
        ):
            if row["id"] not in connected_ids:
                continue
            if curation_by_slug.get(row["slug"]) == "vocab":
                continue
            current = candidates.get(row["id"])
            if current is None or score > current["score"]:
                candidates[row["id"]] = {
                    "row": row,
                    "score": score,
                    "match": "semantic",
                }

    return sorted(
        candidates.values(),
        key=lambda item: (-item["score"], -item["row"]["mention_count"], item["row"]["slug"]),
    )[:limit]


def _expand(
    conn: sqlite3.Connection,
    seed_ids: list[int],
    *,
    depth: int,
    max_nodes: int,
    relations: list[str] | None,
    directed: bool,
) -> tuple[dict[int, int], list[sqlite3.Row]]:
    depth_by_id = {concept_id: 0 for concept_id in seed_ids[:max_nodes]}
    queue: deque[int] = deque(depth_by_id)
    edges: dict[tuple[int, int, str], sqlite3.Row] = {}
    while queue:
        current = queue.popleft()
        current_depth = depth_by_id[current]
        if current_depth >= depth:
            continue
        for edge in _incident_edges(
            conn, current, relations=relations, outgoing_only=directed
        ):
            neighbor = (
                edge["dst_id"] if edge["src_id"] == current else edge["src_id"]
            )
            if neighbor not in depth_by_id:
                if len(depth_by_id) >= max_nodes:
                    continue
                depth_by_id[neighbor] = current_depth + 1
                queue.append(neighbor)
            edges[_edge_key(edge)] = edge
    return depth_by_id, list(edges.values())


def query_subgraph(
    conn: sqlite3.Connection,
    query: str,
    *,
    query_embedding: list[float] | None = None,
    depth: int = 2,
    seed_limit: int = 3,
    max_nodes: int = 30,
    min_similarity: float = 0.4,
    relations: list[str] | None = None,
    evidence_limit: int = 3,
) -> dict:
    """Select lexical/semantic concept seeds and expand their undirected neighborhood."""
    if not query.strip():
        raise ValueError("query must not be empty.")
    depth = _bounded(depth, minimum=0, maximum=4, name="depth")
    seed_limit = _bounded(seed_limit, minimum=1, maximum=10, name="seed_limit")
    max_nodes = _bounded(max_nodes, minimum=1, maximum=100, name="max_nodes")
    evidence_limit = _bounded(
        evidence_limit, minimum=0, maximum=20, name="evidence_limit"
    )
    if not 0.0 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be between 0.0 and 1.0.")

    seeds = _seed_candidates(
        conn,
        query,
        query_embedding=query_embedding,
        limit=min(seed_limit, max_nodes),
        min_similarity=min_similarity,
    )
    seed_ids = [seed["row"]["id"] for seed in seeds]
    depth_by_id, edges = _expand(
        conn,
        seed_ids,
        depth=depth,
        max_nodes=max_nodes,
        relations=relations,
        directed=False,
    )
    return {
        "found": bool(seeds),
        "query": query,
        "relations": _relations(relations),
        "seeds": [
            {
                "concept": _concept_dict(conn, seed["row"]),
                "score": round(seed["score"], 6),
                "match": seed["match"],
            }
            for seed in seeds
        ],
        "nodes": [
            {
                **_concept_dict(conn, _concept_by_id(conn, concept_id)),
                "depth": concept_depth,
                "seed": concept_id in seed_ids,
            }
            for concept_id, concept_depth in depth_by_id.items()
        ],
        "edges": [
            _edge_dict(conn, edge, evidence_limit=evidence_limit) for edge in edges
        ],
    }


def affected(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    max_depth: int = 2,
    max_nodes: int = 30,
    relations: list[str] | None = None,
    evidence_limit: int = 3,
) -> dict:
    """Traverse stored edge direction to return downstream concepts."""
    max_depth = _bounded(max_depth, minimum=1, maximum=6, name="max_depth")
    max_nodes = _bounded(max_nodes, minimum=1, maximum=100, name="max_nodes")
    evidence_limit = _bounded(
        evidence_limit, minimum=0, maximum=20, name="evidence_limit"
    )
    root = gstore.get_concept(conn, identifier)
    if root is None:
        raise ValueError(f"Concept not found: {identifier}")
    depth_by_id, edges = _expand(
        conn,
        [root["id"]],
        depth=max_depth,
        max_nodes=max_nodes,
        relations=relations,
        directed=True,
    )
    return {
        "found": len(depth_by_id) > 1,
        "root": _concept_dict(conn, root),
        "relations": _relations(relations),
        "max_depth": max_depth,
        "nodes": [
            {
                **_concept_dict(conn, _concept_by_id(conn, concept_id)),
                "depth": concept_depth,
            }
            for concept_id, concept_depth in depth_by_id.items()
        ],
        "edges": [
            _edge_dict(
                conn,
                edge,
                evidence_limit=evidence_limit,
                traversed_from=edge["src_id"],
            )
            for edge in edges
        ],
    }
