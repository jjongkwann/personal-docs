"""CrossEncoder를 로드하지 않고 rerank 정렬·컷·빈 입력을 검증."""

from __future__ import annotations

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
