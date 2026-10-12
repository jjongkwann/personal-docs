from sentence_transformers import SentenceTransformer

from pkb.config import resolve_device, settings
from pkb.idle_model import IdleModel


def _load() -> SentenceTransformer:
    device = resolve_device(settings.embedding_device)
    kwargs: dict = {"device": device}
    if settings.embedding_revision:
        kwargs["revision"] = settings.embedding_revision
    if device != "cpu" and settings.embedding_dtype != "float32":
        import torch

        kwargs["model_kwargs"] = {"dtype": getattr(torch, settings.embedding_dtype)}
    try:
        # 캐시에 있으면 허브에 묻지 않는다 — 유휴 해제 뒤 다시 올릴 때마다 요청 40건·6~9초가 들었다
        return SentenceTransformer(settings.embedding_model, local_files_only=True, **kwargs)
    except OSError:  # 캐시에 없다 — 최초 1회 내려받는다
        return SentenceTransformer(settings.embedding_model, **kwargs)


# use()가 적재와 encode를 직렬화한다. 공유 HTTP 서버에서 여러 세션이 동시에 첫 검색을 던져도
# 모델이 중복 로드되지 않고, MPS 할당자는 동시 encode에 안전하지 않다 — 병렬 ingest 툴 호출이
# 겹치면 HeapAllocator::release_available_cached_buffers에서 SIGSEGV (2026-08-19 mini 실측).
_model = IdleModel(_load)


# ingest_files는 한 번의 embed() 호출에 문서 전체의 변경 청크를 모아 넘긴다(수천 개도 가능).
# MPS 캐싱 할당자는 한 encode() 콜 안에서도 내부 배치를 넘길 때마다 블록을 쌓아 두므로,
# 콜이 끝난 뒤 empty_cache()를 해도 콜 도중의 무한 증가는 못 막는다. 그래서 큰 입력은
# 여기서 윈도로 쪼개 encode 사이에 캐시를 비운다(8GB mini에서 재색인이 스왑에 잠기던 문제).
_MPS_ENCODE_WINDOW = 256


def embed(texts: list[str]) -> list[list[float]]:
    """텍스트 리스트를 벡터로 변환."""
    with _model.use() as model:
        is_mps = model.device.type == "mps"
        if is_mps and len(texts) > _MPS_ENCODE_WINDOW:
            import torch

            out: list[list[float]] = []
            for start in range(0, len(texts), _MPS_ENCODE_WINDOW):
                window = texts[start : start + _MPS_ENCODE_WINDOW]
                vectors = model.encode(
                    window,
                    batch_size=settings.embedding_batch_size,
                    show_progress_bar=False,
                )
                out.extend(vectors.tolist())
                torch.mps.empty_cache()
            return out

        vectors = model.encode(
            texts,
            batch_size=settings.embedding_batch_size,
            show_progress_bar=False,
        )
        if is_mps:
            import torch

            torch.mps.empty_cache()
    return vectors.tolist()
