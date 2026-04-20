"""retrieve.py 의 쿼리 DSL 형태 회귀 테스트.

실제 ES 호출 없이 _bm25_query / _knn_query / RRF 결합 로직이 기대한 shape을
유지하는지 검증. ES 쿼리 파서 오류는 런타임에만 나므로 형태 테스트가 가치 있음.
"""

from __future__ import annotations

from pkb.retrieve import RRF_K, _bm25_query, _knn_query

# ---------- _bm25_query ----------

def test_bm25_should_includes_content_title_section():
    q = _bm25_query("hello", None)
    shoulds = q["bool"]["should"]
    fields = {list(s["match"].keys())[0] for s in shoulds}
    assert fields == {"content", "title", "section_path"}


def test_bm25_boosts_content_highest():
    shoulds = _bm25_query("q", None)["bool"]["should"]
    boosts = {list(s["match"].keys())[0]: list(s["match"].values())[0]["boost"] for s in shoulds}
    assert boosts["content"] > boosts["title"] > boosts["section_path"]


def test_bm25_default_includes_lifecycle_filter():
    # include_archived=False 기본 → lifecycle filter 2개 자동 삽입
    q = _bm25_query("q", None)
    filters = q["bool"]["filter"]
    assert len(filters) == 2
    assert {"bool": {"must_not": {"exists": {"field": "archived_at"}}}} in filters


def test_bm25_category_plus_lifecycle():
    q = _bm25_query("q", "study")
    filters = q["bool"]["filter"]
    assert {"term": {"category": "study"}} in filters
    assert len(filters) == 3  # category + 2 lifecycle


def test_bm25_include_archived_drops_filter_when_no_category():
    q = _bm25_query("q", None, include_archived=True)
    assert "filter" not in q["bool"]


def test_bm25_include_archived_keeps_only_category():
    q = _bm25_query("q", "study", include_archived=True)
    assert q["bool"]["filter"] == [{"term": {"category": "study"}}]


# ---------- _knn_query ----------

def test_knn_shape():
    vec = [0.1] * 384
    q = _knn_query(vec, k=10, category=None)
    assert q["field"] == "embedding"
    assert q["k"] == 10
    assert q["num_candidates"] == 50  # k*5
    assert q["query_vector"] is vec
    assert len(q["filter"]) == 2  # lifecycle 기본 포함


def test_knn_with_category_filter():
    q = _knn_query([0.0] * 4, k=5, category="obsidian")
    filters = q["filter"]
    assert {"term": {"category": "obsidian"}} in filters
    assert len(filters) == 3


def test_knn_include_archived_drops_filter():
    q = _knn_query([0.0] * 4, k=5, category=None, include_archived=True)
    assert "filter" not in q


# ---------- RRF_K 상수 ----------

def test_rrf_k_constant():
    # Elastic 기본값 60 — 바뀌면 골든셋 재측정 필요하므로 고정 감시
    assert RRF_K == 60
