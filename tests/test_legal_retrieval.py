"""Legal constraints apply to every retrieval channel and context expansion."""

import pytest

from pkb.retrieve import (
    _bm25_query,
    _knn_query,
    canonical_group_key,
    hybrid_search,
    legal_filter,
)


def test_legal_date_filter_has_known_kind_and_exclusive_statute_end():
    filters = legal_filter(as_of="2024-01-01", law_id="민법")
    assert filters[0] == {"term": {"law_id": "민법"}}
    statute, judgment = filters[1]["bool"]["should"]
    assert statute["bool"]["filter"][:2] == [
        {"term": {"legal_kind": "statute"}}, {"range": {"effective_from": {"lte": "2024-01-01"}}},
    ]
    assert statute["bool"]["filter"][2]["bool"] == {
        "should": [
            {"bool": {"must_not": {"exists": {"field": "effective_to"}}}},
            {"range": {"effective_to": {"gt": "2024-01-01"}}},
        ], "minimum_should_match": 1,
    }
    assert judgment["bool"]["filter"] == [
        {"term": {"legal_kind": "judgment"}}, {"range": {"decision_date": {"lte": "2024-01-01"}}},
    ]


def test_temporal_filter_is_identical_for_bm25_and_knn_and_ignores_todays_lifecycle():
    inputs = {"as_of": "2020-01-01", "article_id": "제12조", "legal_version": "rev-1"}
    bm25 = _bm25_query("q", "legal", profile="source", legal_filters=inputs)["bool"]["filter"]
    knn = _knn_query([0.0], 5, "legal", profile="source", legal_filters=inputs)["filter"]
    assert bm25 == knn
    assert bm25 == [
        {"term": {"category": "legal"}}, {"term": {"doc_type": "source"}}, *legal_filter(**inputs),
    ]


def test_versions_are_distinct_in_canonical_grouping():
    common = {"canonical_id": "civil-12", "law_id": "civil", "article_id": "제12조"}
    old = {**common, "legal_version": "v1", "effective_from": "2020-01-01"}
    new = {**common, "legal_version": "v2", "effective_from": "2024-01-01"}
    assert canonical_group_key(old) != canonical_group_key(new)
    assert canonical_group_key(old) == canonical_group_key({**old, "doc_id": "mirror"})


class CapturingES:
    def __init__(self):
        self.calls = []

    def msearch(self, **kwargs):
        self.calls.append(kwargs)
        source = {
            "doc_id": "old.md", "chunk_index": 1, "content": "old law", "law_id": "law-1",
            "legal_kind": "statute", "legal_version": "v1", "effective_from": "2020-01-01",
            "original_location": "제12조 제2항",
        }
        if "knn" in kwargs["searches"][-1]:
            response = {"hits": {"hits": [{"_id": "old:1", "_score": 1.0, "_source": source}]}}
            return {"responses": [response, response]}
        source["chunk_index"] = 2
        return {"responses": [{"hits": {"hits": [{"_id": "old:2", "_source": source}]}}]}


def test_analysis_filters_reach_all_variants_and_neighbors_with_original_provenance(monkeypatch):
    monkeypatch.setattr("pkb.retrieve.embed", lambda queries: [[0.0] for _ in queries])
    es = CapturingES()
    plans = []
    hits = hybrid_search(
        es, "2021-01-01 기준 제12조 조문?", analyze=True, law_id="law-1", variants=["변형"],
        query_plan_out=plans, expand_context=1, category="legal", profile="source", log=False,
    )
    assert plans[0]["as_of"] == "2021-01-01"
    assert len(es.calls) == 5  # original + three variants + neighbors
    filters = es.calls[0]["searches"][1]["query"]["bool"]["filter"]
    for call in es.calls[:-1]:
        assert call["searches"][1]["query"]["bool"]["filter"] == filters
        assert call["searches"][3]["knn"]["filter"] == filters
    assert es.calls[-1]["searches"][1]["query"]["bool"]["filter"] == filters
    assert hits[0]["neighbors"][0]["original_location"] == "제12조 제2항"
    assert hits[0]["neighbors"][0]["legal_version"] == "v1"
    assert hits[0]["neighbors"][0]["_id"] == "old:2"


@pytest.mark.parametrize("inputs", [
    {"as_of": "today"}, {"law_id": {"match_all": {}}}, {"legal_kind": "all"}, {"issues": ["쟁점"]},
])
def test_invalid_temporal_or_identifier_inputs_fail_before_embedding(monkeypatch, inputs):
    def forbidden(_):
        pytest.fail("invalid filters must fail before embedding")

    monkeypatch.setattr("pkb.retrieve.embed", forbidden)
    with pytest.raises(ValueError):
        hybrid_search(None, "q", log=False, **inputs)
