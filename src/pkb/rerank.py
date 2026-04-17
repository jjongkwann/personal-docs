"""CrossEncoder 기반 다국어 리랭커."""

from sentence_transformers import CrossEncoder

from pkb.config import settings

_reranker: CrossEncoder | None = None


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoder(settings.rerank_model, max_length=512)
    return _reranker


def rerank(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """후보 청크를 CrossEncoder로 재순위. candidates는 hybrid_search 결과."""
    if not candidates:
        return []

    pairs = [(query, c.get("content", "")) for c in candidates]
    scores = get_reranker().predict(pairs, show_progress_bar=False)

    for c, s in zip(candidates, scores, strict=False):
        c["rerank_score"] = float(s)
        c["score"] = float(s)  # rerank 후 점수는 CrossEncoder 기준

    candidates.sort(key=lambda x: -x["rerank_score"])
    return candidates[:top_k]
