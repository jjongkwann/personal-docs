"""CrossEncoder를 로드하지 않고 rerank 정렬·컷·빈 입력을 검증."""

from __future__ import annotations

from pkb import embeddings
from pkb import rerank as rerank_module
from pkb.config import settings
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
    monkeypatch.setattr("pkb.rerank.get_reranker", lambda: model)
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
        "pkb.rerank.get_reranker",
        lambda: (_ for _ in ()).throw(AssertionError("model should not load")),
    )
    assert rerank("query", [], top_k=5) == []


def test_rerank_missing_content_uses_empty_text(monkeypatch):
    model = _FakeReranker([0.2])
    monkeypatch.setattr("pkb.rerank.get_reranker", lambda: model)
    rerank("query", [{}])
    assert model.calls[0][0] == [("query", "")]


def test_rerank_context_is_opt_in_and_preserves_content(monkeypatch):
    model = _FakeReranker([0.2])
    monkeypatch.setattr("pkb.rerank.get_reranker", lambda: model)
    candidate = {"title": "Title", "section_path": "Section", "content": "Body"}
    rerank("query", [candidate], include_context=True)
    assert model.calls[0][0] == [("query", "Title\nSection\nBody")]
    assert candidate["content"] == "Body"


def test_model_loaders_forward_only_configured_revisions(monkeypatch):
    calls = []
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(rerank_module, "_reranker", None)
    monkeypatch.setattr(embeddings, "SentenceTransformer", lambda *a, **kw: calls.append(("embed", kw)))
    monkeypatch.setattr(rerank_module, "CrossEncoder", lambda *a, **kw: calls.append(("rerank", kw)))
    monkeypatch.setattr(embeddings, "resolve_device", lambda _: "cpu")
    monkeypatch.setattr(rerank_module, "resolve_device", lambda _: "cpu")

    monkeypatch.setattr(settings, "embedding_revision", "")
    monkeypatch.setattr(settings, "rerank_revision", "")
    embeddings.get_model()
    rerank_module.get_reranker()
    assert all("revision" not in kwargs for _, kwargs in calls)

    calls.clear()
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(rerank_module, "_reranker", None)
    monkeypatch.setattr(settings, "embedding_revision", "embed-sha")
    monkeypatch.setattr(settings, "rerank_revision", "rerank-sha")
    embeddings.get_model()
    rerank_module.get_reranker()
    assert calls == [
        ("embed", {"device": "cpu", "revision": "embed-sha"}),
        ("rerank", {"max_length": 512, "device": "cpu", "revision": "rerank-sha"}),
    ]
