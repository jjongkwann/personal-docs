import threading

from sentence_transformers import SentenceTransformer

from pkb.config import resolve_device, settings

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()
# MPS 할당자는 동시 encode에 안전하지 않다 — 병렬 ingest 툴 호출이 겹치면
# HeapAllocator::release_available_cached_buffers에서 SIGSEGV (2026-08-19 mini 실측).
_encode_lock = threading.Lock()


def get_model() -> SentenceTransformer:
    """공유 HTTP 서버에서 여러 세션이 동시에 첫 검색을 던지면 모델이 중복 로드될 수 있어 락으로 막는다."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(
                    settings.embedding_model,
                    device=resolve_device(settings.embedding_device),
                )
    return _model


# ingest_files는 한 번의 embed() 호출에 문서 전체의 변경 청크를 모아 넘긴다(수천 개도 가능).
# MPS 캐싱 할당자는 한 encode() 콜 안에서도 내부 배치를 넘길 때마다 블록을 쌓아 두므로,
# 콜이 끝난 뒤 empty_cache()를 해도 콜 도중의 무한 증가는 못 막는다. 그래서 큰 입력은
# 여기서 윈도로 쪼개 encode 사이에 캐시를 비운다(8GB mini에서 재색인이 스왑에 잠기던 문제).
_MPS_ENCODE_WINDOW = 256


def embed(texts: list[str]) -> list[list[float]]:
    """텍스트 리스트를 벡터로 변환."""
    model = get_model()
    is_mps = model.device.type == "mps"
    with _encode_lock:
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
