"""eval.py 순수 함수(dedupe 순위 산출 · recall@k · MRR · miss 리포트) 단위 테스트.

실제 ES 호출 없이 히트 리스트(list of dict)만으로 지표 계산이 맞는지 검증.
"""

from __future__ import annotations

from pkb.eval import doc_ranking, eval_query, format_report, load_gold, mrr, recall_at_k

# ---------- doc_ranking (청크 → 문서 순위 dedupe) ----------

def _hits(*doc_ids: str) -> list[dict]:
    return [{"doc_id": d} for d in doc_ids]


def test_doc_ranking_dedupes_by_first_occurrence():
    hits = _hits("data/a.md", "data/b.md", "data/a.md", "data/c.md", "data/b.md")
    assert doc_ranking(hits) == ["data/a.md", "data/b.md", "data/c.md"]


def test_doc_ranking_empty_hits():
    assert doc_ranking([]) == []


# ---------- eval_query (rank + miss 리포트용 top1) ----------

def test_eval_query_hit_rank_is_doc_level():
    # 청크 2개가 같은 문서 → gold는 문서 순위 기준 2위
    hits = _hits("data/a.md", "data/a.md", "data/gold.md")
    result = eval_query("data/gold.md", hits)
    assert result["rank"] == 2
    assert result["top1"] == "data/a.md"


def test_eval_query_miss_reports_actual_top1():
    # gold가 top 밖 → rank=None, 실제 1위 doc_id를 리포트
    hits = _hits("data/a.md", "data/b.md")
    result = eval_query("data/gold.md", hits)
    assert result["rank"] is None
    assert result["top1"] == "data/a.md"


def test_eval_query_no_hits():
    result = eval_query("data/gold.md", [])
    assert result == {"rank": None, "top1": None}


# ---------- recall@k / MRR ----------

def test_recall_at_k():
    ranks = [1, 3, 6, None]  # 4개 쿼리 중 rank 1, 3, 6, miss
    assert recall_at_k(ranks, 1) == 0.25
    assert recall_at_k(ranks, 3) == 0.5
    assert recall_at_k(ranks, 5) == 0.5
    assert recall_at_k(ranks, 10) == 0.75


def test_recall_empty_ranks():
    assert recall_at_k([], 5) == 0.0


def test_mrr_miss_counts_as_zero():
    # (1/1 + 1/2 + 0) / 3
    assert mrr([1, 2, None]) == (1.0 + 0.5) / 3


def test_mrr_empty_ranks():
    assert mrr([]) == 0.0


# ---------- format_report ----------

def test_format_report_contains_modes_and_misses():
    report = format_report(
        {"bm25": [1, None], "knn": [2, 1]},
        misses=["  [bm25] 질문 → gold data/gold.md, 실제 1위: data/a.md"],
    )
    assert "bm25" in report
    assert "knn" in report
    assert "MRR" in report
    assert "실제 1위: data/a.md" in report


def test_format_report_no_misses_omits_miss_section():
    report = format_report({"bm25": [1]}, misses=[])
    assert "miss" not in report


# ---------- load_gold ----------

def test_load_gold_skips_blank_lines(tmp_path):
    p = tmp_path / "gold.jsonl"
    p.write_text(
        '{"query": "q1", "doc_id": "data/a.md"}\n\n{"query": "q2", "doc_id": "data/b.md"}\n',
        encoding="utf-8",
    )
    assert load_gold(p) == [
        {"query": "q1", "doc_id": "data/a.md"},
        {"query": "q2", "doc_id": "data/b.md"},
    ]
