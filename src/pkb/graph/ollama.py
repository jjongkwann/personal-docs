"""Ollama HTTP contract used by graph extraction; no implicit model downloads."""

from __future__ import annotations

import httpx


def request_json(endpoint: str, path: str, *, payload: dict | None = None, timeout: int = 30) -> dict:
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("Ollama endpoint는 http:// 또는 https:// 주소여야 합니다.")
    try:
        # LAN inference must not be routed through an unrelated shell HTTP proxy.
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            url = endpoint.rstrip("/") + path
            response = client.get(url) if payload is None else client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Ollama {path} 호출 실패: {exc}") from exc
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(f"Ollama {path} 응답 오류: {result}")
    return result


def models(endpoint: str, *, model: str = "", timeout: int = 30) -> list[dict]:
    """Installed tags plus /show capabilities; embedding-only models cannot generate."""
    tags = request_json(endpoint, "/api/tags", timeout=timeout).get("models", [])
    rows = []
    for tag in tags:
        name = tag.get("name", "")
        if model and name != model:
            continue
        info = request_json(endpoint, "/api/show", payload={"model": name}, timeout=timeout)
        capabilities = info.get("capabilities") or []
        context_lengths = [
            value
            for key, value in (info.get("model_info") or {}).items()
            if key.endswith(".context_length") and isinstance(value, int) and value > 0
        ]
        rows.append(
            {
                "name": name,
                "digest": tag.get("digest", ""),
                "capabilities": capabilities,
                "generation": "completion" in capabilities,
                "context_limit": max(context_lengths) if context_lengths else None,
            }
        )
    return rows


def generation_model(endpoint: str, model: str, *, timeout: int = 30) -> dict:
    found = models(endpoint, model=model, timeout=timeout)
    if not found:
        raise ValueError(f"설치된 모델 태그가 없습니다: {model}. `pkb graph models`로 정확한 이름을 확인하세요.")
    selected = found[0]
    if not selected["generation"]:
        raise ValueError(f"생성(completion)을 지원하지 않는 모델입니다: {model}")
    if not selected["digest"]:
        raise ValueError(f"모델 digest가 없어 실행을 추적할 수 없습니다: {model}")
    return selected
