"""질의·문서 임베딩 경로: 기본 설정(bge-m3)은 예전 encode 호출 그대로, 프롬프트 모델은 설정대로."""

from __future__ import annotations

import pytest

from pkb import embeddings
from pkb.config import settings
from pkb.idle_model import IdleModel
from pkb.ingest import embedding_fingerprint

JINA = {
    "embedding_trust_remote_code": True,
    "embedding_task": "retrieval",
    "embedding_query_prompt": "query",
    "embedding_document_prompt": "document",
    "embedding_normalize": True,
}


class _Vectors(list):
    def tolist(self):
        return list(self)


class _FakeModel:
    def __init__(self):
        self.device = type("Device", (), {"type": "cpu"})()
        self.calls: list[tuple[list[str], dict]] = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        return _Vectors([[1.0] for _ in texts])


@pytest.fixture
def model(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(embeddings, "_model", IdleModel(lambda: fake))
    return fake


def test_default_settings_encode_exactly_as_before(model):
    embeddings.embed_documents(["doc"])
    embeddings.embed_queries(["q1", "q2"])
    expected = {"batch_size": settings.embedding_batch_size, "show_progress_bar": False}
    assert model.calls == [(["doc"], expected), (["q1", "q2"], expected)]


def test_prompt_model_settings_split_query_and_document(model, monkeypatch):
    for key, value in JINA.items():
        monkeypatch.setattr(settings, key, value)

    embeddings.embed_documents(["doc"])
    embeddings.embed_queries(["q"])

    (_, doc_kwargs), (_, query_kwargs) = model.calls
    assert doc_kwargs["task"] == query_kwargs["task"] == "retrieval"
    assert doc_kwargs["prompt_name"] == "document"
    assert query_kwargs["prompt_name"] == "query"
    assert doc_kwargs["normalize_embeddings"] is query_kwargs["normalize_embeddings"] is True


def test_trust_remote_code_reaches_the_loader(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(embeddings, "SentenceTransformer", lambda name, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(settings, "embedding_device", "cpu")

    embeddings._load()
    assert "trust_remote_code" not in calls[-1]

    monkeypatch.setattr(settings, "embedding_trust_remote_code", True)
    embeddings._load()
    assert calls[-1]["trust_remote_code"] is True


def test_fingerprint_changes_with_document_encoding_but_not_query_prompt(monkeypatch):
    chunk = {"content": "본문", "title": "", "section_path": ""}
    default = embedding_fingerprint(chunk)

    monkeypatch.setattr(settings, "embedding_query_prompt", "query")
    assert embedding_fingerprint(chunk) == default  # 질의 프롬프트는 저장된 벡터와 무관

    for key in ("embedding_task", "embedding_document_prompt", "embedding_normalize"):
        with monkeypatch.context() as patch:
            patch.setattr(settings, key, JINA[key])
            assert embedding_fingerprint(chunk) != default

    monkeypatch.setattr(settings, "embedding_model", "jinaai/jina-embeddings-v5-text-small")
    assert embedding_fingerprint(chunk) != default
