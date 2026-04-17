"""Golden query set 기반 검색 품질 평가.

JSONL 스키마:
  {
    "id": "rag_01",
    "query": "하이브리드 검색 장점",
    "category": "study",
    "relevant": [
      {"doc_id": "data/study/rag/rag-overview.md", "grade": 3}
    ]
  }

평가 단위는 doc_id입니다. chunk_index까지 고정하면 청킹 변경 때 라벨 유지 비용이 커지므로,
초기 골든셋은 문서 단위 relevance로 둡니다.

실행:
  uv run python scripts/golden_retrieval_eval.py --mode both
  uv run python scripts/golden_retrieval_eval.py --mode rerank --limit 5
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pkb.config import settings  # noqa: E402
from pkb.retrieve import hybrid_search  # noqa: E402
from pkb.store import get_client  # noqa: E402

DEFAULT_QUERIES = ROOT / "data" / ".eval" / "golden_queries.jsonl"


def load_queries(path: Path, limit: int | None = None) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def relevance_map(query: dict) -> dict[str, int]:
    return {
        item["doc_id"]: int(item.get("grade", 1))
        for item in query.get("relevant", [])
        if item.get("doc_id")
    }


def dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades))


def ndcg_at_k(ranked_doc_ids: list[str], relevant: dict[str, int], k: int) -> float:
    actual = [relevant.get(doc_id, 0) for doc_id in ranked_doc_ids[:k]]
    ideal = sorted(relevant.values(), reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return dcg(actual) / ideal_dcg if ideal_dcg else 0.0


def reciprocal_rank(ranked_doc_ids: list[str], relevant: dict[str, int]) -> float:
    for idx, doc_id in enumerate(ranked_doc_ids, start=1):
        if doc_id in relevant:
            return 1.0 / idx
    return 0.0


def dedupe_doc_ids(doc_ids: list[str]) -> list[str]:
    seen = set()
    unique = []
    for doc_id in doc_ids:
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        unique.append(doc_id)
    return unique


def evaluate_query(es, query: dict, *, rerank: bool, top_k: int, candidate_k: int) -> dict:
    category = query.get("category") or None
    results = hybrid_search(
        es,
        query["query"],
        category=category,
        top_k=top_k,
        candidate_k=candidate_k,
        fusion=settings.fusion,
        rerank=rerank,
        log=False,
    )
    raw_doc_ids = [r.get("doc_id", "") for r in results]
    ranked_doc_ids = dedupe_doc_ids(raw_doc_ids)
    relevant = relevance_map(query)
    hit_rank = next(
        (idx for idx, doc_id in enumerate(ranked_doc_ids, start=1) if doc_id in relevant),
        None,
    )
    return {
        "id": query["id"],
        "bucket": query.get("bucket", ""),
        "type": query.get("type", ""),
        "query": query["query"],
        "category": category or "",
        "hit_at_1": ranked_doc_ids[0] in relevant if ranked_doc_ids else False,
        "hit_at_3": any(doc_id in relevant for doc_id in ranked_doc_ids[:3]),
        "hit_at_5": any(doc_id in relevant for doc_id in ranked_doc_ids[:5]),
        "mrr": reciprocal_rank(ranked_doc_ids, relevant),
        "ndcg": ndcg_at_k(ranked_doc_ids, relevant, top_k),
        "hit_rank": hit_rank,
        "top_doc_id": ranked_doc_ids[0] if ranked_doc_ids else "",
    }


def summarize(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"\n[{label}] 평가 결과 없음")
        return

    n = len(rows)
    hit1 = sum(1 for r in rows if r["hit_at_1"]) / n
    hit3 = sum(1 for r in rows if r["hit_at_3"]) / n
    hit5 = sum(1 for r in rows if r["hit_at_5"]) / n
    mrr = statistics.mean(r["mrr"] for r in rows)
    ndcg = statistics.mean(r["ndcg"] for r in rows)

    print(f"\n=== {label} ===")
    print(f"N={n}  Hit@1={hit1:.3f}  Hit@3={hit3:.3f}  Hit@5={hit5:.3f}  MRR={mrr:.3f}  nDCG={ndcg:.3f}")
    print(f"{'id':8s} {'cat':9s} {'h@1':>4s} {'h@3':>4s} {'h@5':>4s} {'mrr':>5s} {'ndcg':>5s} top_doc")
    print("-" * 120)
    for r in rows:
        print(
            f"{r['id']:8s} {r['category'][:9]:9s} "
            f"{str(r['hit_at_1']):>4s} {str(r['hit_at_3']):>4s} "
            f"{str(r['hit_at_5']):>4s} {r['mrr']:5.2f} {r['ndcg']:5.2f} "
            f"{r['top_doc_id']}"
        )


def run(mode: str, queries: list[dict], top_k: int, candidate_k: int) -> None:
    es = get_client()
    modes = [("rrf", False), ("rerank", True)] if mode == "both" else [(mode, mode == "rerank")]
    for label, use_rerank in modes:
        rows = [
            evaluate_query(
                es,
                query,
                rerank=use_rerank,
                top_k=top_k,
                candidate_k=candidate_k,
            )
            for query in queries
        ]
        summarize(rows, label)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--mode", choices=["rrf", "rerank", "both"], default="both")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=settings.candidate_k)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = load_queries(args.queries, limit=args.limit or None)
    run(args.mode, queries, top_k=args.top_k, candidate_k=args.candidate_k)


if __name__ == "__main__":
    main()
