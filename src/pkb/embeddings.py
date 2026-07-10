from sentence_transformers import SentenceTransformer

from pkb.config import resolve_device, settings

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(
            settings.embedding_model,
            device=resolve_device(settings.embedding_device),
        )
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """텍스트 리스트를 벡터로 변환."""
    model = get_model()
    vectors = model.encode(texts, show_progress_bar=False)
    return vectors.tolist()
