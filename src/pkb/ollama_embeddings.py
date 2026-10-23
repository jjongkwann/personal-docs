"""Ollama embedding discovery and validated vectors; never downloads models."""

from __future__ import annotations

import math
from urllib.parse import urlsplit, urlunsplit

from pkb.graph.ollama import request_json

GEMMA_CANONICAL = "google/embeddinggemma-2"
DOCUMENT_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "
# Measured against existing text/concept vectors; architecture alone does not
# prove that a fine-tuned model shares the media vector space.
VERIFIED_GEMMA2_DIGEST = "f4981a15b99858ad1f2e0729416e526be7b4f428291b40082b9e4f7b27b5f62f"


def normalize_endpoint(address: str) -> str:
    address = address.strip()
    explicit_scheme = "://" in address
    if not explicit_scheme:
        address = "http://" + address
    try:
        parts = urlsplit(address)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or any(c.isspace() for c in parts.hostname) or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        netloc = parts.netloc if explicit_scheme or port is not None else parts.netloc + ":11434"
        return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    except ValueError as exc:
        raise ValueError("Ollama IP 또는 http(s) 주소를 입력하세요.") from exc


def _tags(endpoint: str) -> list[dict]:
    rows = request_json(endpoint, "/api/tags").get("models")
    if not isinstance(rows, list):
        raise ValueError("Ollama 모델 목록 응답이 올바르지 않습니다.")
    return rows


def _model_info(endpoint: str, tag: dict) -> dict | None:
    name, digest = tag.get("name"), tag.get("digest")
    if not isinstance(name, str) or not isinstance(digest, str) or not digest:
        raise ValueError("Ollama 모델 이름/digest를 확인하지 못했습니다.")
    show = request_json(endpoint, "/api/show", payload={"model": name})
    if "embedding" not in (show.get("capabilities") or []):
        return None
    info = show.get("model_info") or {}
    architecture = info.get("general.architecture", "")
    dims = info.get(f"{architecture}.embedding_length_out") or info.get(f"{architecture}.embedding_length", 0)
    if type(dims) is not int or dims < 1:
        dims = 0
    # Serving tags and canonical vector spaces are distinct: media uses the latter.
    gemma = architecture == "gemma-embedding2"
    return {
        "name": name, "model": name, "digest": digest, "dimensions": dims,
        "canonical_id": GEMMA_CANONICAL if gemma and digest == VERIFIED_GEMMA2_DIGEST else f"ollama/{name}",
        "document_prefix": DOCUMENT_PREFIX if gemma else "",
        "query_prefix": QUERY_PREFIX if gemma else "",
        "capabilities": show.get("capabilities", []),
    }


def list_models(endpoint: str) -> list[dict]:
    """Metadata only. Selecting a model performs the actual embedding probe."""
    endpoint = normalize_endpoint(endpoint)
    return sorted(
        (info for tag in _tags(endpoint) if (info := _model_info(endpoint, tag)) is not None),
        key=lambda row: row["name"],
    )


def _check_digest(endpoint: str, model: str, expected: str) -> None:
    if not expected:
        raise ValueError("임베딩 모델 digest가 없습니다. 설정에서 모델을 다시 적용하세요.")
    found = next((row for row in _tags(endpoint) if row.get("name") == model), None)
    if found is None or found.get("digest") != expected:
        raise ValueError("Ollama 임베딩 모델이 변경됐습니다. 기존 벡터와 섞이지 않도록 설정에서 다시 적용하세요.")


def _vectors(response: dict, count: int, dimensions: int) -> list[list[float]]:
    vectors = response.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError("Ollama가 요청한 개수와 다른 임베딩을 반환했습니다.")
    result: list[list[float]] = []
    for values in vectors:
        if not isinstance(values, list) or len(values) != dimensions:
            raise ValueError(f"Ollama 임베딩 차원이 색인 차원({dimensions})과 다릅니다.")
        if any(type(value) not in (float, int) or not math.isfinite(value) for value in values):
            raise ValueError("Ollama가 유효하지 않은 임베딩 값을 반환했습니다.")
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm < 1e-12:
            raise ValueError("Ollama가 비어 있는 임베딩 벡터를 반환했습니다.")
        result.append([value / norm for value in values])
    return result


def embed(endpoint: str, model: str, texts: list[str | dict], dimensions: int,
          *, prefix: str = "", expected_digest: str, batch_size: int = 32) -> list[list[float]]:
    """Truncation, malformed vectors and a changed serving tag fail before index writes."""
    if not texts:
        return []
    if dimensions < 1 or batch_size < 1:
        raise ValueError("임베딩 차원과 배치 크기는 양수여야 합니다.")
    endpoint = normalize_endpoint(endpoint)
    _check_digest(endpoint, model, expected_digest)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        inputs = [prefix + text if isinstance(text, str) else text for text in texts[start:start + batch_size]]
        response = request_json(endpoint, "/api/embed", payload={
            "model": model, "input": inputs, "dimensions": dimensions, "truncate": False,
        }, timeout=300)
        vectors.extend(_vectors(response, len(inputs), dimensions))
    _check_digest(endpoint, model, expected_digest)
    return vectors


def inspect_model(endpoint: str, name: str, *, probe: bool = True) -> dict:
    endpoint = normalize_endpoint(endpoint)
    tag = next((tag for tag in _tags(endpoint) if tag.get("name") == name), None)
    selected = _model_info(endpoint, tag) if tag is not None else None
    if selected is None:
        raise ValueError(f"설치된 임베딩 모델이 아닙니다: {name}")
    if probe:
        response = request_json(endpoint, "/api/embed", payload={
            "model": name, "input": [selected["query_prefix"] + "연결 확인"], "truncate": False,
        }, timeout=300)
        rows = response.get("embeddings")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list):
            raise ValueError("Ollama 임베딩 시험 응답이 올바르지 않습니다.")
        selected["dimensions"] = len(rows[0])
        _vectors(response, 1, selected["dimensions"])
        _check_digest(endpoint, name, selected["digest"])
    return {**selected, "endpoint": endpoint}
