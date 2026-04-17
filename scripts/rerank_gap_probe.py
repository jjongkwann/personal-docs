"""Rerank 조건부 스킵 threshold를 잡기 위한 gap 분포/영향 조사.

각 쿼리를 rerank=False, rerank=True로 돌려서:
  - rrf_top_gap (RRF fusion에서 1위-2위 점수차)
  - top1_changed (rerank on/off 간 top-1 doc 변경 여부)
  - top3_overlap (|intersect| / 3)
  - off_total_ms / on_total_ms / rerank_ms_saved

이후 threshold 시뮬레이션:
  gap >= T 일 때 rerank 스킵한다고 가정하면 몇 건이 영향받고, 그중 top1이 실제로
  바뀐(=잘못 스킵된) 건 몇 %인가. 평균 절약 ms는 얼마인가.

실행:
  uv run python scripts/rerank_gap_probe.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pkb.retrieve import hybrid_search  # noqa: E402
from pkb.store import get_client  # noqa: E402

QUERIES_PATH = ROOT / "data" / ".eval" / "queries.jsonl"
REPEATS = 3
TOP_K = 5


def load_queries() -> list[dict]:
    return [json.loads(line) for line in QUERIES_PATH.read_text().splitlines() if line.strip()]


def run_once(es, query: str, rerank: bool) -> tuple[list[tuple], dict]:
    """1회 실행 → (top-5 doc_ids, latency timings dict)."""
    # hybrid_search는 내부에서 log=True로 search_log에 기록하지만 timings는 함수 리턴에 없으므로
    # 직접 재측정. embed/retrieve 세부는 이 스크립트 범위 밖(total만 신경).
    t = perf_counter()
    results = hybrid_search(
        es, query, top_k=TOP_K, rerank=rerank, log=False
    )
    total_ms = (perf_counter() - t) * 1000
    ids = [(r.get("doc_id"), r.get("chunk_index")) for r in results]
    # RRF top-gap 얻기 위해 rerank=False일 때 점수 2개 본다
    rrf_top_gap = 0.0
    if not rerank and len(results) >= 2:
        rrf_top_gap = results[0].get("score", 0) - results[1].get("score", 0)
    return ids, {"total_ms": total_ms, "rrf_top_gap": rrf_top_gap}


def run_query(es, q: dict) -> dict:
    # rerank OFF: 3회, 중앙값 total_ms + top-5 (1회째 기준, 결정론이라 동일)
    off_ids, off_tim = run_once(es, q["query"], rerank=False)
    off_times = [off_tim["total_ms"]]
    for _ in range(REPEATS - 1):
        _, t = run_once(es, q["query"], rerank=False)
        off_times.append(t["total_ms"])

    # rerank ON: 3회, 중앙값
    on_ids, on_tim = run_once(es, q["query"], rerank=True)
    on_times = [on_tim["total_ms"]]
    for _ in range(REPEATS - 1):
        _, t = run_once(es, q["query"], rerank=True)
        on_times.append(t["total_ms"])

    off_top1 = off_ids[0] if off_ids else None
    on_top1 = on_ids[0] if on_ids else None
    off_top3 = set(off_ids[:3])
    on_top3 = set(on_ids[:3])
    top3_overlap = len(off_top3 & on_top3) / 3.0 if on_top3 else 0.0

    return {
        **q,
        "rrf_top_gap": off_tim["rrf_top_gap"],
        "off_top1": off_top1,
        "on_top1": on_top1,
        "top1_changed": off_top1 != on_top1,
        "top3_overlap": top3_overlap,
        "off_total_ms_med": statistics.median(off_times),
        "on_total_ms_med": statistics.median(on_times),
        "rerank_ms_saved": statistics.median(on_times) - statistics.median(off_times),
        "off_top5": off_ids,
        "on_top5": on_ids,
    }


def analyse(records: list[dict]) -> None:
    print("\n" + "=" * 100)
    print(
        f"{'id':8s} {'bucket':10s} {'type':8s} {'gap':>10s} "
        f"{'top1_chg':>9s} {'top3_ov':>8s} {'off_ms':>8s} "
        f"{'on_ms':>8s} {'saved':>7s}"
    )
    print("-" * 100)
    for r in records:
        print(f"{r['id']:8s} {r['bucket']:10s} {r['type']:8s} "
              f"{r['rrf_top_gap']:10.5f} {str(r['top1_changed']):>9s} "
              f"{r['top3_overlap']:8.2f} "
              f"{r['off_total_ms_med']:8.1f} {r['on_total_ms_med']:8.1f} "
              f"{r['rerank_ms_saved']:7.0f}")

    gaps = sorted(r["rrf_top_gap"] for r in records)
    changed_rate = sum(1 for r in records if r["top1_changed"]) / len(records)
    avg_save = statistics.mean(r["rerank_ms_saved"] for r in records)

    print("\n=== 분포 요약 ===")
    print(f"N = {len(records)}")
    print(
        f"rrf_top_gap  min={gaps[0]:.5f}  p25={gaps[len(gaps)//4]:.5f}  "
        f"p50={gaps[len(gaps)//2]:.5f}  p75={gaps[len(gaps)*3//4]:.5f}  "
        f"max={gaps[-1]:.5f}"
    )
    print(f"top-1 변경률 (rerank 유무에 따라 1위 바뀐 쿼리 비율) = {changed_rate*100:.1f}%")
    print(f"rerank로 추가되는 평균 latency = {avg_save:.0f}ms")

    print("\n=== threshold 시뮬레이션 (gap >= T 일 때 rerank 스킵 가정) ===")
    print(f"{'T':>10s} {'skip_n':>7s} {'skip_%':>7s} {'wrong_skip_n':>13s} {'wrong_skip_%':>13s} {'avg_save_ms':>12s}")
    thresholds = [0.0001, 0.0005, 0.001, 0.002, 0.003, 0.005, 0.008, 0.01, 0.02, 0.05]
    for threshold in thresholds:
        skipped = [r for r in records if r["rrf_top_gap"] >= threshold]
        wrong = [r for r in skipped if r["top1_changed"]]
        skip_n = len(skipped)
        wrong_n = len(wrong)
        avg_save_ms = (
            statistics.mean([r["on_total_ms_med"] - r["off_total_ms_med"] for r in skipped])
            if skipped
            else 0.0
        )
        skip_pct = skip_n / len(records) * 100
        wrong_pct = (wrong_n / skip_n * 100) if skip_n else 0.0
        print(
            f"{threshold:10.4f} {skip_n:7d} {skip_pct:6.1f}% "
            f"{wrong_n:13d} {wrong_pct:12.1f}% {avg_save_ms:12.0f}"
        )


def main() -> None:
    es = get_client()
    # 공통 워밍업
    hybrid_search(es, "warmup", top_k=1, rerank=True, log=False)

    queries = load_queries()
    records: list[dict] = []
    for i, q in enumerate(queries, 1):
        print(f"[{i}/{len(queries)}] {q['id']} {q['query']}", flush=True)
        records.append(run_query(es, q))

    out_path = ROOT / "data" / ".eval" / "rerank_gap_probe.jsonl"
    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records))
    print(f"\n결과 저장: {out_path}")

    analyse(records)


if __name__ == "__main__":
    main()
