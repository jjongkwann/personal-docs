"""CrossEncoder 기반 다국어 리랭커."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pkb.config import resolve_device, settings
from pkb.idle_model import IdleModel

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


def _load() -> CrossEncoder:
    from sentence_transformers import CrossEncoder

    kwargs: dict = {"max_length": 512, "device": resolve_device(settings.rerank_device)}
    if settings.rerank_revision:
        kwargs["revision"] = settings.rerank_revision
    try:
        return CrossEncoder(settings.rerank_model, local_files_only=True, **kwargs)  # 캐시에 있으면 허브에 묻지 않는다
    except OSError:  # 캐시에 없다 — 최초 1회 내려받는다
        return CrossEncoder(settings.rerank_model, **kwargs)


# use()가 적재와 predict를 직렬화한다 — 공유 HTTP 서버에서 동시 첫 검색 시 2.3GB 모델이 중복 로드되지 않는다.
_reranker = IdleModel(_load)


def rerank(
    query: str, candidates: list[dict], top_k: int = 5, include_context: bool = False
) -> list[dict]:
    """후보 청크를 CrossEncoder로 재순위. candidates는 hybrid_search 결과."""
    if not candidates:
        return []

    pairs = [
        (
            query,
            "\n".join(p for p in (c.get("title"), c.get("section_path"), c.get("content", "")) if p)
            if include_context else c.get("content", ""),
        )
        for c in candidates
    ]
    with _reranker.use() as model:
        scores = model.predict(
            pairs,
            show_progress_bar=False,
            batch_size=settings.rerank_batch_size,
        )

    for c, s in zip(candidates, scores, strict=False):
        c["rerank_score"] = float(s)
        c["score"] = float(s)  # rerank 후 점수는 CrossEncoder 기준

    candidates.sort(key=lambda x: -x["rerank_score"])
    return candidates[:top_k]
