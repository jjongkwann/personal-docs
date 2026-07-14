"""CrossEncoder 기반 다국어 리랭커."""

import threading

from sentence_transformers import CrossEncoder

from pkb.config import resolve_device, settings

_reranker: CrossEncoder | None = None
_reranker_lock = threading.Lock()


def get_reranker() -> CrossEncoder:
    """공유 HTTP 서버에서 동시 첫 검색 시 2.3GB 모델이 중복 로드되지 않도록 락으로 막는다."""
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                _reranker = CrossEncoder(
                    settings.rerank_model,
                    max_length=512,
                    device=resolve_device(settings.rerank_device),
                )
    return _reranker


def rerank(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """후보 청크를 CrossEncoder로 재순위. candidates는 hybrid_search 결과."""
    if not candidates:
        return []

    pairs = [(query, c.get("content", "")) for c in candidates]
    scores = get_reranker().predict(
        pairs,
        show_progress_bar=False,
        batch_size=settings.rerank_batch_size,
    )

    for c, s in zip(candidates, scores, strict=False):
        c["rerank_score"] = float(s)
        c["score"] = float(s)  # rerank 후 점수는 CrossEncoder 기준

    candidates.sort(key=lambda x: -x["rerank_score"])
    return candidates[:top_k]
