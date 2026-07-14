"""검색·변경 로그 JSONL 기록."""

import json
from datetime import UTC, datetime

from pkb.config import data_dir

LOG_DIR = data_dir() / ".logs"
LOG_FILE = LOG_DIR / "search.jsonl"
CHANGES_FILE = LOG_DIR / "changes.jsonl"
LAST_SYNC_FILE = LOG_DIR / "last_sync.json"  # reconcile이 기록, pkb stale이 읽음


def log_search(
    query: str,
    category: str | None,
    top_k: int,
    fusion: str,
    reranked: bool,
    results: list[dict],
    latency_ms: dict[str, float] | None = None,
    variants: list[str] | None = None,
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "query": query,
        "category": category,
        "top_k": top_k,
        "fusion": fusion,
        "reranked": reranked,
        "latency_ms": latency_ms or {},
        "results": [
            {
                "doc_id": r.get("doc_id"),
                "chunk_index": r.get("chunk_index"),
                "score": r.get("score"),
                "rerank_score": r.get("rerank_score"),
            }
            for r in results
        ],
    }
    if variants:
        entry["variants"] = variants  # RAG-Fusion 쿼리 변형 — 없으면 기록 생략
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_change(op: str, doc_id: str, **detail) -> None:
    """문서 변이(ingest/delete/archive/restore/purge)를 changes.jsonl에 기록.

    변이가 로그 실패로 죽으면 안 되므로 예외는 내부에서 전부 무시한다.
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "op": op,
            "doc_id": doc_id,
            **detail,
        }
        with CHANGES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 로깅 실패는 변이를 막지 않음
