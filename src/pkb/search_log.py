"""검색 로그 JSONL 기록."""

import json
from datetime import UTC, datetime
from pathlib import Path

LOG_DIR = Path.cwd() / "data" / ".logs"
LOG_FILE = LOG_DIR / "search.jsonl"


def log_search(
    query: str,
    category: str | None,
    top_k: int,
    fusion: str,
    reranked: bool,
    results: list[dict],
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "query": query,
        "category": category,
        "top_k": top_k,
        "fusion": fusion,
        "reranked": reranked,
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
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
