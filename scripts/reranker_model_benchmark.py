"""Reranker 모델 × candidate_k 매트릭스 벤치마크.

golden_queries.jsonl을 기반으로 다음 축을 직교 평가한다:
  - reranker_model : v2-m3 / base (+ 비교용 RRF-only 베이스라인)
  - candidate_k    : 20 / 30 / 50

각 조합에 대해 쿼리별 Hit@k, MRR, nDCG, total_ms를 측정하고 집계 표를 출력.
Per-query raw는 data/.eval/reranker_model_benchmark.jsonl 로 남긴다.

모델 스위칭:
  pkb.rerank 가 모듈 전역 싱글톤 `_reranker`를 쓰므로, 설정값(settings.rerank_model)
  을 바꾸고 `_reranker = None`으로 초기화해 다음 get_reranker()에서 새 모델을 로드.

실행:
  uv run python scripts/reranker_model_benchmark.py
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pkb import rerank as rerank_module  # noqa: E402
from pkb.config import settings  # noqa: E402
from pkb.retrieve import hybrid_search  # noqa: E402
from pkb.store import get_client  # noqa: E402

QUERIES = ROOT / "data" / ".eval" / "golden_queries.jsonl"
OUT_PATH = ROOT / "data" / ".eval" / "reranker_model_benchmark.jsonl"
REPEATS = 2  # latency 중앙값 산정용


# ------- 평가 유틸 (golden_retrieval_eval.py와 동일 로직 — 중복 허용) -------

def dcg(grades: list[int]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at_k(ranked_doc_ids: list[str], rel: dict[str, int], k: int) -> float:
    actual = [rel.get(d, 0) for d in ranked_doc_ids[:k]]
    ideal = sorted(rel.values(), reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return dcg(actual) / ideal_dcg if ideal_dcg else 0.0


def reciprocal_rank(ranked: list[str], rel: dict[str, int]) -> float:
    for idx, d in enumerate(ranked, start=1):
        if d in rel:
            return 1.0 / idx
    return 0.0


def dedupe(doc_ids: list[str]) -> list[str]:
    seen, out = set(), []
    for d in doc_ids:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def relevance_map(q: dict) -> dict[str, int]:
    return {item["doc_id"]: int(item.get("grade", 1)) for item in q.get("relevant", []) if item.get("doc_id")}


# ------- 모델 스위칭 -------

def switch_reranker(model_name: str | None) -> None:
    """model_name=None 이면 rerank 비활성(RRF only)."""
    if model_name is None:
        return
    settings.rerank_model = model_name
    rerank_module._reranker = None  # force reload


def warmup_reranker() -> None:
    """현재 설정된 rerank 모델을 로드하고 2회 추론으로 예열."""
    rr = rerank_module.get_reranker()
    rr.predict(
        [("w", "w")] * 4,
        show_progress_bar=False,
        batch_size=settings.rerank_batch_size,
    )


# ------- 한 조합 실행 -------

def run_config(
    es,
    queries: list[dict],
    *,
    model_name: str | None,
    candidate_k: int,
    top_k: int = 5,
) -> list[dict]:
    use_rerank = model_name is not None
    if use_rerank:
        switch_reranker(model_name)
        warmup_reranker()

    rows: list[dict] = []
    for q in queries:
        rel = relevance_map(q)
        category = q.get("category") or None

        # 1회: 품질 지표용 (결정론)
        res = hybrid_search(
            es, q["query"], category=category, top_k=top_k,
            candidate_k=candidate_k, fusion=settings.fusion,
            rerank=use_rerank, log=False,
        )
        ranked = dedupe([r.get("doc_id", "") for r in res])

        # 반복: latency 중앙값
        lats = []
        for _ in range(REPEATS):
            t = perf_counter()
            hybrid_search(
                es, q["query"], category=category, top_k=top_k,
                candidate_k=candidate_k, fusion=settings.fusion,
                rerank=use_rerank, log=False,
            )
            lats.append((perf_counter() - t) * 1000)

        rows.append({
            "id": q["id"],
            "bucket": q.get("bucket", ""),
            "type": q.get("type", ""),
            "hit_at_1": bool(ranked and ranked[0] in rel),
            "hit_at_3": any(d in rel for d in ranked[:3]),
            "hit_at_5": any(d in rel for d in ranked[:5]),
            "mrr": reciprocal_rank(ranked, rel),
            "ndcg": ndcg_at_k(ranked, rel, top_k),
            "total_ms_med": statistics.median(lats),
            "top3": ranked[:3],
        })
    return rows


# ------- 요약 -------

def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    return {
        "n": n,
        "hit_at_1": sum(r["hit_at_1"] for r in rows) / n,
        "hit_at_3": sum(r["hit_at_3"] for r in rows) / n,
        "hit_at_5": sum(r["hit_at_5"] for r in rows) / n,
        "mrr": statistics.mean(r["mrr"] for r in rows),
        "ndcg": statistics.mean(r["ndcg"] for r in rows),
        "total_ms_avg": statistics.mean(r["total_ms_med"] for r in rows),
        "total_ms_med": statistics.median([r["total_ms_med"] for r in rows]),
    }


def main() -> None:
    queries = [json.loads(x) for x in QUERIES.read_text().splitlines() if x.strip()]
    print(f"골든셋 쿼리: {len(queries)}개\n")

    es = get_client()
    # 공통 워밍업 (임베딩 모델 + ES 컨넥션)
    hybrid_search(es, "warmup", top_k=1, rerank=False, log=False)

    configs = [
        # (label, model_name_or_None, candidate_k)
        ("RRF-only ck=20", None, 20),
        ("RRF-only ck=50", None, 50),
        ("v2-m3 ck=20", "BAAI/bge-reranker-v2-m3", 20),
        ("v2-m3 ck=30", "BAAI/bge-reranker-v2-m3", 30),
        ("v2-m3 ck=50", "BAAI/bge-reranker-v2-m3", 50),
        ("base   ck=20", "BAAI/bge-reranker-base", 20),
        ("base   ck=30", "BAAI/bge-reranker-base", 30),
        ("base   ck=50", "BAAI/bge-reranker-base", 50),
    ]

    all_rows: list[dict] = []
    summary: list[dict] = []
    for label, model, ck in configs:
        print(f"[{label}] 실행 중...", flush=True)
        rows = run_config(es, queries, model_name=model, candidate_k=ck)
        for r in rows:
            all_rows.append({**r, "config": label, "model": model or "none", "candidate_k": ck})
        agg = aggregate(rows)
        summary.append({"label": label, "model": model or "none", "candidate_k": ck, **agg})

    # JSONL 저장
    OUT_PATH.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows))
    print(f"\nper-query 결과 저장: {OUT_PATH}\n")

    # Summary 표
    print("=" * 110)
    header = (
        f"{'config':30s} {'Hit@1':>7s} {'Hit@3':>7s} {'Hit@5':>7s} "
        f"{'MRR':>6s} {'nDCG':>6s} {'tot_ms_med':>11s} {'tot_ms_avg':>11s}"
    )
    print(header)
    print("-" * 110)
    for s in summary:
        print(f"{s['label']:30s} "
              f"{s['hit_at_1']:7.3f} {s['hit_at_3']:7.3f} {s['hit_at_5']:7.3f} "
              f"{s['mrr']:6.3f} {s['ndcg']:6.3f} "
              f"{s['total_ms_med']:11.0f} {s['total_ms_avg']:11.0f}")

    # baseline 대비 변화 (RRF-only ck=50 기준)
    base = next((s for s in summary if s["label"] == "RRF-only ck=50"), None)
    if base:
        base_ms = base["total_ms_med"]
        print("\n=== baseline(RRF-only ck=50) 대비 변화 ===")
        for s in summary:
            if s is base:
                continue
            d_ndcg = s["ndcg"] - base["ndcg"]
            d_mrr = s["mrr"] - base["mrr"]
            d_hit1 = s["hit_at_1"] - base["hit_at_1"]
            s_ms = s["total_ms_med"]
            d_ms = s_ms - base_ms
            ratio = s_ms / base_ms if base_ms else 0.0
            if ratio >= 1.05:
                speed_str = f"{ratio:.1f}x slower"
            elif ratio > 0 and ratio <= 0.95:
                speed_str = f"{1 / ratio:.1f}x faster"
            else:
                speed_str = "~same"
            print(f"  {s['label']:30s}  ΔnDCG={d_ndcg:+.3f}  ΔMRR={d_mrr:+.3f}  ΔHit@1={d_hit1:+.3f}  "
                  f"latency {s_ms:.0f}ms ({d_ms:+.0f}ms, {speed_str})")


if __name__ == "__main__":
    main()
