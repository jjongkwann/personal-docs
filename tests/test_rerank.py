"""CrossEncoder를 로드하지 않고 rerank 정렬·컷·빈 입력을 검증."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from pkb import embeddings
from pkb import rerank as rerank_module
from pkb.config import settings
from pkb.idle_model import IdleModel
from pkb.rerank import rerank


class _FakeReranker:
    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append((pairs, kwargs))
        return self.scores


def test_rerank_sorts_scores_and_applies_top_k(monkeypatch):
    model = _FakeReranker([0.1, 0.9, -0.2])
    monkeypatch.setattr(rerank_module, "_reranker", IdleModel(lambda: model))
    candidates = [
        {"content": "a", "score": 10.0},
        {"content": "b", "score": 9.0},
        {"content": "c", "score": 8.0},
    ]

    result = rerank("query", candidates, top_k=2)

    assert [row["content"] for row in result] == ["b", "a"]
    assert [row["rerank_score"] for row in result] == [0.9, 0.1]
    assert result[0]["score"] == 0.9
    assert model.calls[0][0] == [("query", "a"), ("query", "b"), ("query", "c")]
    assert model.calls[0][1]["show_progress_bar"] is False


def test_rerank_empty_candidates_does_not_load_model(monkeypatch):
    monkeypatch.setattr(
        rerank_module,
        "_reranker",
        IdleModel(lambda: (_ for _ in ()).throw(AssertionError("model should not load"))),
    )
    assert rerank("query", [], top_k=5) == []


def test_rerank_missing_content_uses_empty_text(monkeypatch):
    model = _FakeReranker([0.2])
    monkeypatch.setattr(rerank_module, "_reranker", IdleModel(lambda: model))
    rerank("query", [{}])
    assert model.calls[0][0] == [("query", "")]


def test_rerank_context_is_opt_in_and_preserves_content(monkeypatch):
    model = _FakeReranker([0.2])
    monkeypatch.setattr(rerank_module, "_reranker", IdleModel(lambda: model))
    candidate = {"title": "Title", "section_path": "Section", "content": "Body"}
    rerank("query", [candidate], include_context=True)
    assert model.calls[0][0] == [("query", "Title\nSection\nBody")]
    assert candidate["content"] == "Body"


def test_model_loaders_forward_only_configured_revisions(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=lambda *a, **kw: calls.append(("embed", kw)),
        CrossEncoder=lambda *a, **kw: calls.append(("rerank", kw)),
    ))
    monkeypatch.setattr(embeddings, "resolve_device", lambda _: "cpu")
    monkeypatch.setattr(rerank_module, "resolve_device", lambda _: "cpu")

    monkeypatch.setattr(settings, "embedding_revision", "")
    monkeypatch.setattr(settings, "rerank_revision", "")
    embeddings._load()
    rerank_module._load()
    assert all("revision" not in kwargs for _, kwargs in calls)

    calls.clear()
    monkeypatch.setattr(settings, "embedding_revision", "embed-sha")
    monkeypatch.setattr(settings, "rerank_revision", "rerank-sha")
    embeddings._load()
    rerank_module._load()
    assert calls == [
        ("embed", {"local_files_only": True, "device": "cpu", "revision": "embed-sha"}),
        ("rerank", {"local_files_only": True, "max_length": 512, "device": "cpu", "revision": "rerank-sha"}),
    ]


def test_embedding_loader_uses_half_precision_only_on_gpu(monkeypatch):
    import torch

    calls = []
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda *a, **kw: calls.append(kw))
    )
    monkeypatch.setattr(settings, "embedding_revision", "")

    monkeypatch.setattr(embeddings, "resolve_device", lambda _: "mps")
    monkeypatch.setattr(settings, "embedding_dtype", "float16")
    embeddings._load()
    monkeypatch.setattr(settings, "embedding_dtype", "float32")
    embeddings._load()
    monkeypatch.setattr(embeddings, "resolve_device", lambda _: "cpu")
    monkeypatch.setattr(settings, "embedding_dtype", "float16")
    embeddings._load()

    assert calls == [
        {"local_files_only": True, "device": "mps", "model_kwargs": {"dtype": torch.float16}},
        {"local_files_only": True, "device": "mps"},
        {"local_files_only": True, "device": "cpu"},
    ]


def test_model_loaders_download_when_not_cached(monkeypatch):
    calls = []

    def loader(name):
        def load(*args, **kwargs):
            calls.append((name, kwargs.get("local_files_only", False)))
            if kwargs.get("local_files_only"):
                raise OSError("not cached")
            return name

        return load

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=loader("embed"), CrossEncoder=loader("rerank"),
    ))
    monkeypatch.setattr(embeddings, "resolve_device", lambda _: "cpu")
    monkeypatch.setattr(rerank_module, "resolve_device", lambda _: "cpu")

    assert embeddings._load() == "embed"
    assert rerank_module._load() == "rerank"
    assert calls == [("embed", True), ("embed", False), ("rerank", True), ("rerank", False)]
