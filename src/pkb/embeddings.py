import threading

from sentence_transformers import SentenceTransformer

from pkb.config import resolve_device, settings

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


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


def embed(texts: list[str]) -> list[list[float]]:
    """텍스트 리스트를 벡터로 변환."""
    model = get_model()
    vectors = model.encode(
        texts,
        batch_size=settings.embedding_batch_size,
        show_progress_bar=False,
    )
    return vectors.tolist()
