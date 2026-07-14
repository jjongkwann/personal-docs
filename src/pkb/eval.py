"""검색 품질 평가 하니스 (pkb eval).

data/.eval/gold.jsonl의 (query, doc_id) 골드셋을 4개 검색 모드
(bm25 단독 / knn 단독 / rrf / rrf+rerank)로 돌려 recall@k와 MRR을 비교한다.
순위 산출·지표 계산은 ES 없이 단위 테스트 가능한 순수 함수로 분리.
"""

from __future__ import annotations

import json
from pathlib import Path

from elasticsearch import Elasticsearch

from pkb.config import settings

TOP_K = 10  # 문서 단위 평가 컷오프 (recall@10까지 산출)
# 모드별로 가져올 청크 수. 모든 모드가 같은 깊이·같은 선택 규칙(_cap_per_doc)으로 후보를
# 만들어야 문서 단위 recall 비교가 공정하다 — 모드별로 다르면 랭킹 품질이 아니라 후보
# 구성 차이를 재게 된다. dedupe 후 문서 10개를 채우기 위해 TOP_K보다 넉넉히 잡는다.
FETCH_K = 40
RECALL_KS = (1, 3, 5, 10)
MODES = ("bm25", "knn", "rrf", "rrf+rerank")


# ---------- 순수 함수 (ES 불필요) ----------

def load_gold(path: Path) -> list[dict]:
    """gold.jsonl 로드 — 라인당 {"query": str, "doc_id": str}. 빈 줄 무시."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def doc_ranking(hits: list[dict]) -> list[str]:
    """청크 히트 목록을 doc_id 첫 등장 순서로 dedupe한 문서 순위 리스트로 변환."""
    return list(dict.fromkeys(h["doc_id"] for h in hits))


def eval_query(gold_doc_id: str, hits: list[dict]) -> dict:
    """단일 쿼리 평가 — rank는 골드 문서의 1-기반 순위(순위 밖이면 None),
    top1은 miss 리포트용 실제 1위 doc_id(결과 없으면 None)."""
    ranking = doc_ranking(hits)
    rank = ranking.index(gold_doc_id) + 1 if gold_doc_id in ranking else None
    return {"rank": rank, "top1": ranking[0] if ranking else None}


def recall_at_k(ranks: list[int | None], k: int) -> float:
    """골드 문서가 상위 k 안에 든 쿼리 비율. miss(None)는 실패로 계산."""
    if not ranks:
        return 0.0
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mrr(ranks: list[int | None]) -> float:
    """Mean Reciprocal Rank. miss(None)는 0으로 계산."""
    if not ranks:
        return 0.0
    return sum(1.0 / r for r in ranks if r is not None) / len(ranks)


def format_report(ranks_by_mode: dict[str, list[int | None]], misses: list[str]) -> str:
    """모드×지표 표 + miss 목록을 문자열로 렌더."""
    header = f"{'모드':<12}" + "".join(f"{f'R@{k}':>8}" for k in RECALL_KS) + f"{'MRR':>8}"
    lines = [header, "-" * len(header)]
    for mode, ranks in ranks_by_mode.items():
        row = f"{mode:<12}"
        for k in RECALL_KS:
            row += f"{recall_at_k(ranks, k):>8.3f}"
        row += f"{mrr(ranks):>8.3f}"
        lines.append(row)
    if misses:
        lines.append("")
        lines.append(f"miss {len(misses)}건 (gold가 top{TOP_K} 밖):")
        lines.extend(misses)
    return "\n".join(lines)


# ---------- 검색 실행 (ES 필요) ----------

def _run_modes(es: Elasticsearch, query_text: str) -> dict[str, list[dict]]:
    """한 쿼리를 4개 모드로 검색해 모드별 히트(청크 source) 리스트 반환.

    네 모드 모두 FETCH_K개 청크를 같은 다양성 캡으로 선택한다 — 이후 doc_ranking이
    문서 단위로 dedupe하므로 후보 풀 구성이 모드 간 동일해진다.
    """
    from pkb.embeddings import embed
    from pkb.retrieve import _bm25_query, _cap_per_doc, _knn_query, hybrid_search

    bm25 = es.search(
        index=settings.es_index,
        query=_bm25_query(query_text, None),
        size=FETCH_K,
        source_excludes=["embedding"],
    )
    knn = es.search(
        index=settings.es_index,
        knn=_knn_query(embed([query_text])[0], FETCH_K, None),
        size=FETCH_K,
        source_excludes=["embedding"],
    )
    return {
        "bm25": _cap_per_doc([h["_source"] for h in bm25["hits"]["hits"]], FETCH_K),
        "knn": _cap_per_doc([h["_source"] for h in knn["hits"]["hits"]], FETCH_K),
        "rrf": hybrid_search(
            es, query_text, top_k=FETCH_K, candidate_k=FETCH_K, rerank=False, log=False
        ),
        "rrf+rerank": hybrid_search(
            es, query_text, top_k=FETCH_K, candidate_k=FETCH_K, rerank=True, log=False
        ),
    }


def evaluate(es: Elasticsearch, gold: list[dict]) -> str:
    """골드셋 전체를 4개 모드로 평가해 리포트 문자열 반환."""
    # ponytail: 쿼리당 4회 순차 검색 — 수십 개 골드셋 규모엔 충분, 느려지면 임베딩 배치화
    ranks_by_mode: dict[str, list[int | None]] = {m: [] for m in MODES}
    misses: list[str] = []
    for row in gold:
        hits_by_mode = _run_modes(es, row["query"])
        for mode in MODES:
            result = eval_query(row["doc_id"], hits_by_mode[mode])
            ranks_by_mode[mode].append(result["rank"])
            if result["rank"] is None or result["rank"] > TOP_K:
                misses.append(
                    f"  [{mode}] {row['query']} → gold {row['doc_id']}, "
                    f"실제 1위: {result['top1'] or '(결과 없음)'}"
                )
    return format_report(ranks_by_mode, misses)
