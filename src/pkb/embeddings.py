from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from pkb.config import resolve_device, settings
from pkb.idle_model import IdleModel

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


def _load() -> SentenceTransformer:
    from sentence_transformers import SentenceTransformer

    device = resolve_device(settings.embedding_device)
    kwargs: dict = {"device": device}
    if settings.embedding_revision:
        kwargs["revision"] = settings.embedding_revision
    if settings.embedding_trust_remote_code:
        kwargs["trust_remote_code"] = True
    if settings.embedding_text_only:
        kwargs["config_kwargs"] = {"vision_config": None, "audio_config": None}
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


# ingest_files는 한 번의 embed_documents() 호출에 문서 전체의 변경 청크를 모아 넘긴다(수천 개도 가능).
# MPS 캐싱 할당자는 한 encode() 콜 안에서도 내부 배치를 넘길 때마다 블록을 쌓아 두므로,
# 콜이 끝난 뒤 empty_cache()를 해도 콜 도중의 무한 증가는 못 막는다. 그래서 큰 입력은
# 여기서 윈도로 쪼개 encode 사이에 캐시를 비운다(8GB mini에서 재색인이 스왑에 잠기던 문제).
_MPS_ENCODE_WINDOW = 256


def _encode_options(prompt_name: str) -> dict:
    """설정이 켠 encode 인자만. 기본 설정이면 빈 dict라 bge-m3 벡터가 예전과 같다."""
    options: dict = {}
    if settings.embedding_task:
        options["task"] = settings.embedding_task
    if prompt_name:
        options["prompt_name"] = prompt_name
    if settings.embedding_normalize:
        options["normalize_embeddings"] = True
    return options


@contextlib.contextmanager
def _padded_to_multiple(model: SentenceTransformer, multiple: int) -> Iterator[None]:
    """encode 동안 토큰 길이를 multiple의 배수로 늘려 입력 모양의 가짓수를 줄인다.

    MPS는 입력 모양마다 버퍼를 따로 잡아 두어, 길이가 제각각인 청크를 대량으로 임베딩하면
    empty_cache()를 해도 드라이버 메모리가 계속 는다 (jina v5, 256청크 묶음 4개: 7.5→17.5GB).
    128 배수로 맞추면 5.4GB에서 멈춘다. 늘린 자리는 attention_mask 0이라 벡터는 그대로다.
    MPS가 아니거나 multiple이 0이면 아무것도 바꾸지 않는다.
    """
    if not multiple or model.device.type != "mps":
        yield
        return
    preprocess = model.preprocess
    tokenizer = model.tokenizer

    def padded(*args, **kwargs):
        features = preprocess(*args, **kwargs)
        length = features["input_ids"].shape[1]
        extra = -length % multiple
        if extra:
            from torch.nn.functional import pad

            side = (extra, 0) if tokenizer.padding_side == "left" else (0, extra)
            for key, value in features.items():
                if hasattr(value, "dim") and value.dim() == 2 and value.shape[1] == length:
                    fill = tokenizer.pad_token_id if key == "input_ids" else 0
                    features[key] = pad(value, side, value=fill)
        return features

    model.preprocess = padded
    try:
        yield
    finally:
        del model.preprocess


def _embed(texts: list[str], options: dict, pad_to_multiple_of: int = 0) -> list[list[float]]:
    with _model.use() as model, _padded_to_multiple(model, pad_to_multiple_of):
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
                    **options,
                )
                out.extend(vectors.tolist())
                torch.mps.empty_cache()
            return out

        vectors = model.encode(
            texts,
            batch_size=settings.embedding_batch_size,
            show_progress_bar=False,
            **options,
        )
        if is_mps:
            import torch

            torch.mps.empty_cache()
    return vectors.tolist()


def embed_documents(texts: list[str], *, pad_to_multiple_of: int = 0) -> list[list[float]]:
    """색인할 텍스트(청크·개념 설명)를 벡터로 변환.

    pad_to_multiple_of>0이면 MPS에서 토큰 길이를 그 배수로 맞춘다 — 전량 재임베딩처럼 길이가 다른
    입력을 오래 넣을 때 메모리가 늘지 않게 (_padded_to_multiple).
    """
    if settings.embedding_provider == "ollama":
        return _ollama_embed(texts, query=False)
    return _embed(texts, _encode_options(settings.embedding_document_prompt), pad_to_multiple_of)


def embed_queries(texts: list[str]) -> list[list[float]]:
    """검색 질의 목록을 벡터로 변환. 질의 변형을 한 번에 배치 인코딩한다."""
    if settings.embedding_provider == "ollama":
        return _ollama_embed(texts, query=True)
    return _embed(texts, _encode_options(settings.embedding_query_prompt))


def _ollama_embed(texts: list[str], *, query: bool) -> list[list[float]]:
    from pkb.ollama_embeddings import embed

    prefix = settings.embedding_ollama_query_prefix if query else settings.embedding_ollama_document_prefix
    return embed(settings.embedding_ollama_endpoint, settings.embedding_ollama_model, texts,
                 settings.embedding_dims, prefix=prefix, expected_digest=settings.embedding_ollama_digest,
                 batch_size=settings.embedding_batch_size)
