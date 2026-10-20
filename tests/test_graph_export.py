"""질의 시 전이 추론과 RDF(OWL·SKOS) 내보내기 테스트."""

from __future__ import annotations

import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from pkb.graph import export, ontology
from pkb.graph import query as gquery
from pkb.graph import store as gstore
from pkb.graph.schema import get_connection, init_schema


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "graph.sqlite")
    init_schema(path)
    connection = get_connection(path)
    ids = {
        name: gstore.upsert_concept(connection, name=name, description=f"{name} 설명", kind="technique")
        for name in ("X", "Y", "Z", "W", "Vector Search", "검색")
    }
    for src, dst in (("X", "Y"), ("Y", "Z"), ("Z", "W")):
        gstore.add_edge(connection, ids[src], ids[dst], "is_a", doc_id="d", chunk_index=0)
    gstore.add_edge(connection, ids["검색"], ids["Vector Search"], "related_to", doc_id="d", chunk_index=0)
    gstore.add_alias(connection, ids["Vector Search"], "벡터 검색")
    yield connection
    connection.close()


# ---------- 추론 ----------


def test_closure_returns_only_inferred_edges_with_premise_paths(conn):
    up = gquery.transitive_closure(conn, "X", "is_a")
    assert [(item["target"]["slug"], item["hops"], item["via"]) for item in up["inferred"]] == [
        ("z", 2, ["x", "y", "z"]),
        ("w", 3, ["x", "y", "z", "w"]),
    ]
    assert all(item["inferred"] and item["source"]["slug"] == "x" for item in up["inferred"])

    down = gquery.transitive_closure(conn, "W", "is_a", direction="down")
    assert [(item["source"]["slug"], item["via"]) for item in down["inferred"]] == [
        ("y", ["y", "z", "w"]),
        ("x", ["x", "y", "z", "w"]),
    ]
    assert len(gquery.transitive_closure(conn, "X", "is_a", max_depth=2)["inferred"]) == 1
    with pytest.raises(ValueError):
        gquery.transitive_closure(conn, "X", "related_to")


def test_explain_adds_kind_inverse_labels_and_inferred(conn):
    result = gquery.explain(conn, "Y")
    assert result["concept"]["kind"] == "technique"
    assert result["inbound"][0]["inverse_relation"] == "has_subtype"
    assert result["outbound"][0]["target"]["kind"] == "technique"
    assert [item["target"]["slug"] for item in result["inferred"]] == ["w"]


def test_directed_traversal_follows_symmetric_relations_both_ways(conn):
    # 검색(id 6) related_to Vector Search(id 5)는 (5, 6)으로 저장된다
    result = gquery.affected(conn, "검색", relations=["related_to"])
    assert [node["slug"] for node in result["nodes"]] == ["검색", "vector search"]
    assert result["edges"][0]["traversal_direction"] == "reverse"


# ---------- RDF ----------


def test_tbox_owl_axioms():
    graph = export.tbox_graph()
    ns = export._tbox_ns(export.DEFAULT_BASE)

    def typed(kind):
        return {node for node in graph.subjects(RDF.type, kind) if isinstance(node, URIRef)}

    assert typed(OWL.TransitiveProperty) == {ns[r] for r in ontology.TRANSITIVE_RELATIONS}
    assert typed(OWL.SymmetricProperty) == {ns[r] for r in ontology.SYMMETRIC_RELATIONS}
    assert typed(OWL.AsymmetricProperty) == {ns.implements}
    # OWL 2 DL: 비반사는 단순(비전이) 속성에만
    assert typed(OWL.IrreflexiveProperty) == {ns[r] for r in ontology.RELATION_IDS} - typed(OWL.TransitiveProperty)
    assert len(list(graph.subject_objects(OWL.inverseOf))) == 6
    assert len(typed(OWL.Class)) == len(ontology.KINDS) + len(ontology.GROUPS) + 1
    disjoint = next(graph.subjects(RDF.type, OWL.AllDisjointClasses))
    assert len(list(graph.items(graph.value(disjoint, OWL.members)))) == len(ontology.KINDS)
    assert (ns.is_a, OWL.inverseOf, ns.has_subtype) not in graph
    assert (ns.has_subtype, OWL.inverseOf, ns.is_a) in graph
    assert (ns.is_a, export.RDFS.subPropertyOf, SKOS.broader) in graph
    assert (ns.uses, export.RDFS.subPropertyOf, SKOS.related) in graph
    assert graph.value(URIRef("urn:pkb:tbox"), OWL.versionIRI) == URIRef("urn:pkb:tbox/1")


def test_turtle_and_trig_round_trip(conn, tmp_path):
    turtle = export.export_rdf(conn, tmp_path / "graph.ttl")
    assert turtle["concepts"] == 6 and turtle["edges"] == 4 and turtle["skipped_edges"] == {}
    graph = Graph().parse(tmp_path / "graph.ttl", format="turtle")
    assert len(graph) == turtle["triples"]["tbox"] + turtle["triples"]["abox"]

    ns = export._tbox_ns(export.DEFAULT_BASE)
    vector = URIRef("urn:pkb:concept/vector%20search")
    search = export.concept_iri("검색")
    assert str(search) == "urn:pkb:concept/%EA%B2%80%EC%83%89"
    assert (vector, RDF.type, ns.technique) in graph and (vector, RDF.type, SKOS.Concept) in graph
    assert (vector, SKOS.prefLabel, Literal("Vector Search")) in graph
    assert (vector, SKOS.altLabel, Literal("벡터 검색")) in graph
    assert (vector, SKOS.definition, Literal("Vector Search 설명")) in graph
    assert (vector, ns.related_to, search) in graph
    assert len(list(graph.subjects(SKOS.inScheme, None))) == 6

    trig = export.export_rdf(conn, tmp_path / "graph.trig", fmt="trig")
    dataset = Dataset().parse(tmp_path / "graph.trig", format="trig")
    inferred = dataset.graph(URIRef("urn:pkb:graph/inferred"))
    assert trig["triples"]["inferred"] == len(inferred) == 3  # x→z, x→w, y→w
    x, w = export.concept_iri("x"), export.concept_iri("w")
    assert (x, ns.is_a, w) in inferred and (x, ns.is_a, w) not in dataset.default_graph
    with pytest.raises(ValueError):
        export.export_rdf(conn, tmp_path / "graph.nt", fmt="nt")
