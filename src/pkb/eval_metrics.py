"""검색 평가 지표 — goldens 기반 Hit@k / MRR / nDCG 계산.

scripts/golden_retrieval_eval.py, scripts/reranker_model_benchmark.py가 공통으로 사용.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def dcg(grades: Sequence[int]) -> float:
    """Discounted Cumulative Gain (log2(rank+1) 감쇠)."""
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at_k(ranked_doc_ids: Sequence[str], relevant: dict[str, int], k: int) -> float:
    """Normalized DCG@k. relevant이 비어있거나 ideal_dcg가 0이면 0.0 반환."""
    actual = [relevant.get(d, 0) for d in ranked_doc_ids[:k]]
    ideal = sorted(relevant.values(), reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return dcg(actual) / ideal_dcg if ideal_dcg else 0.0


def reciprocal_rank(ranked_doc_ids: Sequence[str], relevant: dict[str, int]) -> float:
    """Reciprocal Rank: 첫 relevant 문서 등장 위치의 역수. 없으면 0.0."""
    for idx, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id in relevant:
            return 1.0 / idx
    return 0.0


def hit_at_k(ranked_doc_ids: Sequence[str], relevant: dict[str, int], k: int) -> bool:
    """top-k 내에 적어도 하나의 relevant 문서가 있는지."""
    return any(d in relevant for d in ranked_doc_ids[:k])


def dedupe_doc_ids(doc_ids: Sequence[str]) -> list[str]:
    """빈 문자열 제거 + 순서 유지 중복 제거."""
    seen: set[str] = set()
    out: list[str] = []
    for d in doc_ids:
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def relevance_map(query: dict) -> dict[str, int]:
    """golden_queries row의 relevant 리스트 → {doc_id: grade} dict."""
    return {
        item["doc_id"]: int(item.get("grade", 1))
        for item in query.get("relevant", [])
        if item.get("doc_id")
    }
