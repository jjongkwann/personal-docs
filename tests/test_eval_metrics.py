"""pkb.eval_metrics 단위 테스트."""

from __future__ import annotations

import math

import pytest

from pkb.eval_metrics import (
    dcg,
    dedupe_doc_ids,
    hit_at_k,
    ndcg_at_k,
    reciprocal_rank,
    relevance_map,
)

# ---------- dcg ----------

def test_dcg_empty_returns_zero():
    assert dcg([]) == 0.0


def test_dcg_single_grade_at_first():
    # (2^3 - 1) / log2(1+1) = 7 / 1 = 7
    assert dcg([3]) == pytest.approx(7.0)


def test_dcg_multi_position_decay():
    expected = 7 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
    assert dcg([3, 2, 1]) == pytest.approx(expected)


def test_dcg_zero_grade_contributes_nothing():
    assert dcg([0, 0, 0]) == 0.0


# ---------- ndcg_at_k ----------

def test_ndcg_perfect_ranking_is_one():
    rel = {"a": 3, "b": 2, "c": 1}
    assert ndcg_at_k(["a", "b", "c"], rel, k=3) == pytest.approx(1.0)


def test_ndcg_reversed_ranking_less_than_one():
    rel = {"a": 3, "b": 2, "c": 1}
    score = ndcg_at_k(["c", "b", "a"], rel, k=3)
    assert 0 < score < 1


def test_ndcg_no_relevant_in_topk_returns_zero():
    rel = {"x": 3}
    assert ndcg_at_k(["a", "b", "c"], rel, k=3) == 0.0


def test_ndcg_empty_relevant_returns_zero():
    assert ndcg_at_k(["a"], {}, k=1) == 0.0


def test_ndcg_k_limits_evaluation_window():
    rel = {"a": 3}
    # k=1: 정답 없음 (a가 rank 2에) / k=2: 정답 잡힘
    assert ndcg_at_k(["x", "a"], rel, k=1) == 0.0
    assert ndcg_at_k(["x", "a"], rel, k=2) > 0.0


# ---------- reciprocal_rank ----------

def test_rr_first_position_is_one():
    assert reciprocal_rank(["a", "b"], {"a": 1}) == 1.0


def test_rr_third_position_is_one_third():
    assert reciprocal_rank(["a", "b", "c"], {"c": 1}) == pytest.approx(1.0 / 3)


def test_rr_not_in_ranked_returns_zero():
    assert reciprocal_rank(["a", "b"], {"x": 1}) == 0.0


def test_rr_uses_first_relevant_match():
    # b(rank=2), c(rank=3)가 모두 relevant일 때 1/2
    rr = reciprocal_rank(["a", "b", "c"], {"b": 1, "c": 1})
    assert rr == pytest.approx(0.5)


# ---------- hit_at_k ----------

def test_hit_at_k_respects_window():
    rel = {"b": 2}
    assert hit_at_k(["a", "b", "c"], rel, k=2) is True
    assert hit_at_k(["a", "c", "b"], rel, k=2) is False  # b는 rank 3
    assert hit_at_k(["a", "c", "b"], rel, k=3) is True


def test_hit_at_k_no_relevant_returns_false():
    assert hit_at_k(["a", "b"], {}, k=5) is False


# ---------- dedupe_doc_ids ----------

def test_dedupe_preserves_order():
    assert dedupe_doc_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_dedupe_drops_empty_strings():
    assert dedupe_doc_ids(["", "a", "", "a"]) == ["a"]


def test_dedupe_empty_input():
    assert dedupe_doc_ids([]) == []


# ---------- relevance_map ----------

def test_relevance_map_standard_shape():
    q = {"relevant": [
        {"doc_id": "a", "grade": 3, "reason": "x"},
        {"doc_id": "b", "grade": 2},
    ]}
    assert relevance_map(q) == {"a": 3, "b": 2}


def test_relevance_map_default_grade_is_one():
    assert relevance_map({"relevant": [{"doc_id": "a"}]}) == {"a": 1}


def test_relevance_map_skips_missing_doc_id():
    q = {"relevant": [{"doc_id": "a", "grade": 3}, {"grade": 2}]}
    assert relevance_map(q) == {"a": 3}


def test_relevance_map_empty_relevant():
    assert relevance_map({"relevant": []}) == {}
    assert relevance_map({}) == {}
