"""개념 그래프를 RDF로 내보낸다: TBox는 OWL, 개념(ABox)은 SKOS.

- Turtle: TBox + 단언된 개념·관계.
- TriG: 위 내용은 기본 그래프, 전이 관계(is_a·part_of·prerequisite_of)의 추론 폐포는 이름 있는 그래프
  `<base>graph/inferred`. 추론 관계는 근거가 아니며 SQLite에 저장하지 않는다.

IRI는 base(기본 `urn:pkb:`) 아래에 둔다. 클래스·속성 `<base>tbox#<id>`, 개념 `<base>concept/<slug>`
(slug를 퍼센트 인코딩, 표시 이름은 쓰지 않는다). 받은 연결에서 한정자 없는 SELECT만 한다.

OWL 2 DL은 비반사(irreflexive)·비대칭 특성을 단순 속성에만 허용하고 전이 속성은 단순하지 않다. 그래서
전이 관계에는 owl:TransitiveProperty만 두고 비순환(따라서 비반사)은 주석 pkbo:acyclic으로 남긴다. 두 끝의
쌍 조건(같은 kind·같은 그룹)은 OWL 공리로 옮기지 않고 주석 pkbo:signature로 남긴다.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from pkb.graph import ontology
from pkb.graph.validate import reachable

DEFAULT_BASE = "urn:pkb:"
ISOTHES = Namespace("http://purl.org/iso25964/skos-thes#")
FORMATS = ("turtle", "trig")


def _tbox_ns(base: str) -> Namespace:
    return Namespace(f"{base}tbox#")


def concept_iri(slug: str, base: str = DEFAULT_BASE) -> URIRef:
    return URIRef(f"{base}concept/{quote(slug, safe='')}")


def _class_expr(graph: Graph, ns: Namespace, kinds: frozenset[str]):
    """kind 집합의 클래스 표현: 한 클래스(kind 또는 그룹 전체), 아니면 owl:unionOf."""
    members = []
    for group in ontology.GROUPS:
        group_kinds = ontology.kinds_of(group)
        if group_kinds <= kinds:
            members.append(ns[group])
        else:
            members.extend(ns[kind] for kind in ontology.KIND_IDS if kind in kinds & group_kinds)
    if len(members) == 1:
        return members[0]
    node, items = BNode(), BNode()
    Collection(graph, items, members)
    graph.add((node, RDF.type, OWL.Class))
    graph.add((node, OWL.unionOf, items))
    return node


def add_tbox(graph: Graph, base: str = DEFAULT_BASE) -> None:
    """TBox를 OWL 공리로 graph에 더한다."""
    ns = _tbox_ns(base)
    ontology_iri = URIRef(f"{base}tbox")
    graph.add((ontology_iri, RDF.type, OWL.Ontology))
    graph.add((ontology_iri, OWL.versionIRI, URIRef(f"{base}tbox/{ontology.TBOX_VERSION.rsplit('/', 1)[-1]}")))
    graph.add((ontology_iri, OWL.versionInfo, Literal(ontology.TBOX_VERSION)))
    for annotation in ("acyclic", "signature"):
        graph.add((ns[annotation], RDF.type, OWL.AnnotationProperty))

    root = ns[ontology.ROOT_KIND]
    graph.add((root, RDF.type, OWL.Class))
    graph.add((root, RDFS.subClassOf, SKOS.Concept))
    graph.add((root, RDFS.label, Literal(ontology.ROOT_KIND, lang="en")))
    for group in ontology.GROUPS.values():
        graph.add((ns[group.id], RDF.type, OWL.Class))
        graph.add((ns[group.id], RDFS.subClassOf, root))
        graph.add((ns[group.id], RDFS.label, Literal(group.id, lang="en")))
        graph.add((ns[group.id], RDFS.label, Literal(group.label_ko, lang="ko")))
        graph.add((ns[group.id], RDFS.comment, Literal(group.definition_en, lang="en")))
        graph.add((ns[group.id], RDFS.comment, Literal(group.definition_ko, lang="ko")))
    for kind in ontology.KINDS.values():
        node = ns[kind.id]
        graph.add((node, RDF.type, OWL.Class))
        graph.add((node, RDFS.subClassOf, ns[kind.group]))
        graph.add((node, RDFS.subClassOf, root))
        graph.add((node, RDFS.label, Literal(kind.id, lang="en")))
        graph.add((node, RDFS.label, Literal(kind.label_ko, lang="ko")))
        graph.add((node, RDFS.comment, Literal(kind.definition_en, lang="en")))
        graph.add((node, RDFS.comment, Literal(kind.definition_ko, lang="ko")))
    disjoint, members = BNode(), BNode()
    Collection(graph, members, [ns[kind] for kind in ontology.KIND_IDS])
    graph.add((disjoint, RDF.type, OWL.AllDisjointClasses))
    graph.add((disjoint, OWL.members, members))

    for relation in ontology.RELATIONS.values():
        prop = ns[relation.id]
        graph.add((prop, RDF.type, OWL.ObjectProperty))
        graph.add((prop, RDFS.label, Literal(relation.id, lang="en")))
        graph.add((prop, RDFS.label, Literal(relation.label_ko, lang="ko")))
        graph.add((prop, RDFS.comment, Literal(relation.definition_en, lang="en")))
        graph.add((prop, RDFS.comment, Literal(relation.definition_ko, lang="ko")))
        graph.add((prop, ns.signature, Literal(ontology.signature_text(relation))))
        if relation.transitive:
            graph.add((prop, RDF.type, OWL.TransitiveProperty))
        if relation.symmetric:
            graph.add((prop, RDF.type, OWL.SymmetricProperty))
        if relation.asymmetric:
            graph.add((prop, RDF.type, OWL.AsymmetricProperty))
        if relation.irreflexive and not relation.transitive:
            graph.add((prop, RDF.type, OWL.IrreflexiveProperty))
        if relation.acyclic:
            graph.add((prop, ns.acyclic, Literal(True)))
        if relation.inverse:
            inverse = ns[relation.inverse]
            graph.add((inverse, RDF.type, OWL.ObjectProperty))
            graph.add((inverse, RDFS.label, Literal(relation.inverse, lang="en")))
            graph.add((inverse, OWL.inverseOf, prop))
        graph.add((prop, RDFS.subPropertyOf, SKOS.broader if relation.skos == "broader" else SKOS.related))
        if relation.id == "is_a":
            graph.add((prop, RDFS.subPropertyOf, ISOTHES.broaderGeneric))
        if relation.id == "part_of":
            graph.add((prop, RDFS.subPropertyOf, ISOTHES.broaderPartitive))
        if relation.domain != frozenset(ontology.KIND_IDS):
            graph.add((prop, RDFS.domain, _class_expr(graph, ns, relation.domain)))
        if relation.range != frozenset(ontology.KIND_IDS):
            graph.add((prop, RDFS.range, _class_expr(graph, ns, relation.range)))


def tbox_graph(base: str = DEFAULT_BASE) -> Graph:
    graph = Graph()
    _bind(graph, base)
    add_tbox(graph, base)
    return graph


def _bind(graph: Graph | Dataset, base: str) -> None:
    graph.bind("pkbo", _tbox_ns(base))
    graph.bind("pkbc", Namespace(f"{base}concept/"))
    graph.bind("skos", SKOS)
    graph.bind("owl", OWL)
    graph.bind("isothes", ISOTHES)


def add_abox(graph: Graph, conn: sqlite3.Connection, base: str = DEFAULT_BASE) -> dict:
    """개념을 skos:Concept + kind 클래스로, 단언된 관계를 트리플로 더한다. 반환: 개수 통계."""
    ns = _tbox_ns(base)
    scheme = URIRef(f"{base}scheme")
    graph.add((scheme, RDF.type, SKOS.ConceptScheme))
    graph.add((scheme, SKOS.prefLabel, Literal("PKB concepts", lang="en")))
    aliases: dict[int, list[str]] = {}
    for concept_id, alias in conn.execute("SELECT concept_id, alias FROM concept_aliases ORDER BY concept_id, alias"):
        aliases.setdefault(concept_id, []).append(alias)
    iri_by_id: dict[int, URIRef] = {}
    for row in conn.execute("SELECT id, slug, name, description, concept_type FROM concepts ORDER BY id"):
        iri = concept_iri(row["slug"], base)
        iri_by_id[row["id"]] = iri
        graph.add((iri, RDF.type, SKOS.Concept))
        kind = row["concept_type"] if ontology.get_kind(row["concept_type"]) else ontology.ROOT_KIND
        graph.add((iri, RDF.type, ns[kind]))
        graph.add((iri, SKOS.inScheme, scheme))
        graph.add((iri, SKOS.prefLabel, Literal(row["name"])))
        for alias in aliases.get(row["id"], ()):
            if alias != row["name"]:  # SKOS S13: prefLabel과 altLabel은 겹치지 않는다
                graph.add((iri, SKOS.altLabel, Literal(alias)))
        if row["description"]:
            graph.add((iri, SKOS.definition, Literal(row["description"])))
    skipped: Counter[str] = Counter()
    edges = 0
    for src_id, dst_id, relation in conn.execute("SELECT src_id, dst_id, relation FROM concept_edges"):
        if ontology.get_relation(relation) is None or src_id not in iri_by_id or dst_id not in iri_by_id:
            skipped[relation] += 1
            continue
        graph.add((iri_by_id[src_id], ns[relation], iri_by_id[dst_id]))
        edges += 1
    return {"concepts": len(iri_by_id), "edges": edges, "skipped_edges": dict(skipped)}


def add_inferred(graph: Graph, conn: sqlite3.Connection, base: str = DEFAULT_BASE) -> int:
    """전이 관계의 폐포에서 단언되지 않은 트리플만 더한다. 반환: 더한 트리플 수."""
    ns = _tbox_ns(base)
    slug_by_id = dict(conn.execute("SELECT id, slug FROM concepts"))
    added = 0
    for relation in sorted(ontology.TRANSITIVE_RELATIONS):
        adjacency: dict[int, set[int]] = {}
        for src_id, dst_id in conn.execute(
            "SELECT src_id, dst_id FROM concept_edges WHERE relation = ? AND src_id != dst_id", (relation,)
        ):
            adjacency.setdefault(src_id, set()).add(dst_id)
        for src_id in sorted(adjacency):
            for node in reachable(adjacency, src_id):
                if node == src_id or node in adjacency[src_id] or node not in slug_by_id:
                    continue
                graph.add((concept_iri(slug_by_id[src_id], base), ns[relation], concept_iri(slug_by_id[node], base)))
                added += 1
    return added


def export_rdf(
    conn: sqlite3.Connection,
    destination: str | Path,
    *,
    fmt: str = "turtle",
    base: str = DEFAULT_BASE,
) -> dict:
    """그래프를 RDF 파일로 쓴다. fmt: turtle(TBox + 단언) | trig(+ 추론 폐포 이름 있는 그래프)."""
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {FORMATS}.")
    if not base or base[-1] not in ":/#":
        raise ValueError("base는 ':', '/' 또는 '#'로 끝나는 IRI 접두어여야 합니다 (예: urn:pkb:).")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "turtle":
        graph = Graph()
        _bind(graph, base)
        add_tbox(graph, base)
        tbox_triples = len(graph)
        stats = add_abox(graph, conn, base)
        triples = {"tbox": tbox_triples, "abox": len(graph) - tbox_triples}
        graph.serialize(destination=str(path), format="turtle")
    else:
        dataset = Dataset()
        _bind(dataset, base)
        default = dataset.default_graph
        add_tbox(default, base)
        tbox_triples = len(default)
        stats = add_abox(default, conn, base)
        inferred = dataset.graph(URIRef(f"{base}graph/inferred"))
        triples = {
            "tbox": tbox_triples,
            "abox": len(default) - tbox_triples,
            "inferred": add_inferred(inferred, conn, base),
        }
        dataset.serialize(destination=str(path), format="trig")
    return {
        "format": fmt,
        "path": str(path),
        "base": base,
        "tbox_version": ontology.TBOX_VERSION,
        **stats,
        "triples": triples,
    }

