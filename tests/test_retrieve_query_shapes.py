"""retrieve.py 의 쿼리 DSL 형태 회귀 테스트.

실제 ES 호출 없이 _bm25_query / _knn_query / RRF 결합 로직이 기대한 shape을
유지하는지 검증. ES 쿼리 파서 오류는 런타임에만 나므로 형태 테스트가 가치 있음.
"""

from __future__ import annotations

import pytest

from pkb.retrieve import RRF_K, _bm25_query, _knn_query, hybrid_search

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


# ---------- exclude_doc_prefix (코퍼스 밖 볼트 문서 배제 검색) ----------

def test_bm25_exclude_doc_prefix_adds_must_not_prefix():
    q = _bm25_query("q", None, exclude_doc_prefix="obsidian/")
    filters = q["bool"]["filter"]
    assert {"bool": {"must_not": [{"prefix": {"doc_id": "obsidian/"}}]}} in filters


def test_bm25_no_exclude_doc_prefix_by_default():
    q = _bm25_query("q", None)
    filters = q["bool"]["filter"]
    exclude_filter = {"bool": {"must_not": [{"prefix": {"doc_id": "obsidian/"}}]}}
    assert exclude_filter not in filters


def test_knn_exclude_doc_prefix_adds_must_not_prefix():
    q = _knn_query([0.0] * 4, k=5, category=None, exclude_doc_prefix="obsidian/")
    filters = q["filter"]
    assert {"bool": {"must_not": [{"prefix": {"doc_id": "obsidian/"}}]}} in filters


def test_knn_no_exclude_doc_prefix_by_default():
    q = _knn_query([0.0] * 4, k=5, category=None)
    filters = q["filter"]
    exclude_filter = {"bool": {"must_not": [{"prefix": {"doc_id": "obsidian/"}}]}}
    assert exclude_filter not in filters


# ---------- RRF_K 상수 ----------

def test_rrf_k_constant():
    # Elastic 기본값 60 — 바뀌면 골든셋 재측정 필요하므로 고정 감시
    assert RRF_K == 60


# ---------- hybrid_search 다중 쿼리 변형 융합 (RAG-Fusion) ----------

def _patch_fusion(monkeypatch, hits_by_query: dict[str, list[dict]]) -> dict:
    """embed/_rrf_search 스텁 — ES·임베딩 없이 hybrid_search의 융합 로직만 실행."""
    calls: dict = {"embed": [], "queries": [], "vectors": []}

    def fake_embed(texts):
        calls["embed"].append(list(texts))
        # 쿼리 인덱스별 구분 벡터 — 쿼리↔임베딩 매핑 역전 회귀 감시용
        return [[float(i)] * 4 for i in range(len(texts))]

    def fake_rrf(es, query_text, query_vector, category, candidate_k, **kwargs):
        calls["queries"].append(query_text)
        calls["vectors"].append(query_vector)
        return [dict(h) for h in hits_by_query.get(query_text, [])]

    monkeypatch.setattr("pkb.retrieve.embed", fake_embed)
    monkeypatch.setattr("pkb.retrieve._rrf_search", fake_rrf)
    return calls


def test_hybrid_search_variants_merge_sums_scores(monkeypatch):
    calls = _patch_fusion(monkeypatch, {
        "원질의": [
            {"_id": "a", "doc_id": "data/x.md", "score": 0.5},
            {"_id": "b", "doc_id": "data/y.md", "score": 0.4},
        ],
        "변형1": [
            {"_id": "b", "doc_id": "data/y.md", "score": 0.3},
        ],
    })
    results = hybrid_search(None, "원질의", variants=["변형1"], log=False)
    assert calls["embed"] == [["원질의", "변형1"]]  # 배치 인코딩 1회
    assert calls["vectors"] == [[0.0] * 4, [1.0] * 4]  # 원 쿼리·변형이 각자 자기 벡터를 받음
    assert [r["_id"] for r in results] == ["b", "a"]  # b: 0.4+0.3 > a: 0.5
    assert results[0]["score"] == pytest.approx(0.7)


def test_hybrid_search_variants_dedupes_and_strips(monkeypatch):
    calls = _patch_fusion(monkeypatch, {})
    # " q " → strip 후 원 쿼리와 중복, "  " → 빈 문자열, "v1" 중복 — 전부 제거
    hybrid_search(None, "q", variants=[" q ", "v1", "v1", "  ", "v2"], log=False)
    assert calls["queries"] == ["q", "v1", "v2"]


def test_hybrid_search_variants_capped_at_three(monkeypatch):
    calls = _patch_fusion(monkeypatch, {})
    hybrid_search(None, "q", variants=["v1", "v2", "v3", "v4"], log=False)
    assert calls["queries"] == ["q", "v1", "v2", "v3"]  # 변형은 최대 3개


def test_hybrid_search_no_variants_single_query(monkeypatch):
    calls = _patch_fusion(monkeypatch, {
        "q": [{"_id": "a", "doc_id": "data/x.md", "score": 0.5}],
    })
    results = hybrid_search(None, "q", log=False)
    assert calls["queries"] == ["q"]
    assert [r["_id"] for r in results] == ["a"]


def test_hybrid_search_exposes_original_query_vector_without_reencoding(monkeypatch):
    calls = _patch_fusion(monkeypatch, {})
    captured = []
    hybrid_search(None, "q", variants=["v1"], log=False, query_vector_out=captured)
    assert calls["embed"] == [["q", "v1"]]
    assert captured == [[0.0] * 4]
