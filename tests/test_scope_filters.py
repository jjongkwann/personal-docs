"""검색 범위 필터(doc_ids·source_types·people·기간·경로)가 BM25·kNN 양쪽에 같게 들어가는지."""

from __future__ import annotations

import pytest

from pkb.retrieve import _bm25_query, _knn_query, hybrid_search, scope_filter


def test_scope_filter_builds_clauses_on_shared_field_names():
    filters = scope_filter(
        doc_ids=["data/a.md", "data/b.md"],
        source_types=["Meeting"],
        people=["Alice@Example.com", "김철수"],
        date_from="2026-10-01",
        date_to="2026-10-15",
        path_prefix="data/meetings/",
    )
    assert filters == [
        {"terms": {"doc_id": ["data/a.md", "data/b.md"]}},
        {"terms": {"source_type": ["meeting"]}},
        {"terms": {"people": ["alice@example.com", "김철수"]}},
        # 끝 날짜는 그날 끝까지 포함
        {"range": {"occurred_at": {"gte": "2026-10-01", "lte": "2026-10-15||/d"}}},
        {"prefix": {"doc_id": "data/meetings/"}},
    ]


def test_scope_filter_keeps_datetimes_and_treats_empty_as_unrestricted():
    assert scope_filter(date_to="2026-10-15T09:30:00+09:00") == [
        {"range": {"occurred_at": {"lte": "2026-10-15T09:30:00+09:00"}}}
    ]
    assert scope_filter(doc_ids=[], source_types=None, people=[]) == []


@pytest.mark.parametrize("kwargs", [
    {"date_from": "10/15/2026"},
    {"date_to": "2026-13-01"},
    {"doc_ids": ["data/a.md", ""]},
    {"doc_ids": "data/a.md"},
    {"path_prefix": "  "},
])
def test_scope_filter_rejects_invalid_input(kwargs):
    with pytest.raises(ValueError):
        scope_filter(**kwargs)


def test_bm25_and_knn_carry_the_same_scope_filters():
    scope = scope_filter(doc_ids=["data/a.md"])
    bm25 = _bm25_query("q", None, scope_filters=scope)
    knn = _knn_query([0.0] * 4, 5, None, scope_filters=scope)
    assert {"terms": {"doc_id": ["data/a.md"]}} in bm25["bool"]["filter"]
    assert {"terms": {"doc_id": ["data/a.md"]}} in knn["filter"]
    # 생명주기 필터는 그대로 남는다
    assert len(bm25["bool"]["filter"]) == 3
    assert bm25["bool"]["filter"] == knn["filter"]


def test_omitted_scope_leaves_query_unchanged():
    assert _bm25_query("q", "study") == _bm25_query("q", "study", scope_filters=[])
    assert _knn_query([0.0], 5, None) == _knn_query([0.0], 5, None, scope_filters=None)


class _FakeES:
    def __init__(self):
        self.searches: list[list[dict]] = []

    def msearch(self, index, searches):
        self.searches.append(searches)
        if len(searches) == 4 and "knn" in searches[3]:
            hit = {"_id": "data/a.md_0", "_score": 1.0, "_source": {"doc_id": "data/a.md", "chunk_index": 0}}
            return {"responses": [{"hits": {"hits": [hit]}}, {"hits": {"hits": [hit]}}]}
        return {"responses": [{"hits": {"hits": []}}]}


def test_hybrid_search_applies_scope_to_bm25_knn_and_neighbors(monkeypatch):
    monkeypatch.setattr("pkb.retrieve.embed_queries", lambda texts: [[0.0] for _ in texts])
    es = _FakeES()

    hybrid_search(
        es, "q", log=False, expand_context=1,
        doc_ids=["data/a.md"], people=["A@x.com"], date_from="2026-10-01",
    )

    rrf, neighbors = es.searches
    expected = [
        {"terms": {"doc_id": ["data/a.md"]}},
        {"terms": {"people": ["a@x.com"]}},
        {"range": {"occurred_at": {"gte": "2026-10-01"}}},
    ]
    bm25_filters = rrf[1]["query"]["bool"]["filter"]
    knn_filters = rrf[3]["knn"]["filter"]
    neighbor_filters = neighbors[1]["query"]["bool"]["filter"]
    for clause in expected:
        assert clause in bm25_filters
        assert clause in knn_filters
        assert clause in neighbor_filters


def test_hybrid_search_validates_scope_before_embedding(monkeypatch):
    def forbidden(texts):
        raise AssertionError("embedding must not run")

    monkeypatch.setattr("pkb.retrieve.embed_queries", forbidden)
    with pytest.raises(ValueError):
        hybrid_search(None, "q", log=False, date_to="yesterday")
