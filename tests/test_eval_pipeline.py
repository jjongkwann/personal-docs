"""Benchmark routes every experiment through operational hybrid_search."""
import json

import pytest

from pkb import eval as evaluation


def test_evaluate_uses_same_gold_and_full_search_configuration(monkeypatch):
    calls = []

    def search(es, query, **kwargs):
        calls.append((es, query, kwargs))
        return [{"doc_id": "a", "chunk_index": 1}] if query == "answer" else []

    monkeypatch.setattr(evaluation, "hybrid_search", search)
    gold = [
        {"version": 2, "query": "answer", "query_type": "fact", "answerable": True,
         "relevant": [{"doc_id": "a", "chunk_index": 1}], "variants": ["alternate"]},
        {"version": 2, "query": "unknown", "query_type": "noanswer", "answerable": False,
         "relevant": []},
    ]
    es = object()
    report = evaluation.evaluate(es, gold, configurations={
        "base": {}, "linear": {"fusion": "linear", "lexical_weight": 0.7, "rerank_context": True},
    }, top_k=5)
    assert len(calls) == 4
    assert all(call[0] is es for call in calls)
    assert [call[2]["variants"] for call in calls] == [["alternate"], None, ["alternate"], None]
    assert all(call[2]["log"] is False and call[2]["canonical_group"] is True for call in calls)
    assert calls[2][2]["fusion"] == "linear"
    assert calls[2][2]["lexical_weight"] == 0.7
    assert calls[2][2]["rerank_context"] is True
    assert calls[0][2]["candidate_k"] == evaluation.settings.candidate_k
    assert calls[0][2]["exclude_doc_prefix"] is None
    assert report["modes"]["base"]["summary"]["final_recall_at_k"] == 1
    assert report["modes"]["base"]["summary"]["noanswer_false_positive_rate"] == 0
    assert report["modes"]["base"]["by_query_type"]["fact"]["evidence_recall_at_k"] == 1
    assert len(report["metadata"]["queryset_sha256"]) == 64
    assert "final R@5" in evaluation.format_report(report)
    json.dumps(report)


@pytest.mark.parametrize("bad", [
    {"rerank": "false"}, {"canonical_group": 1}, {"rerank_context": "true"},
    {"include_archived": 0}, {"candidate_k": 0}, {"candidate_k": True},
    {"expand_context": -1}, {"canonical_boost": -0.1}, {"canonical_boost": 1.0},
    {"lexical_weight": "0.5"}, {"lexical_weight": 2},
    {"profile": []}, {"fusion": "typo"}, {"exclude_doc_prefix": False},
])
def test_invalid_configuration_fails_before_any_search(monkeypatch, bad):
    calls = []
    monkeypatch.setattr(evaluation, "hybrid_search", lambda *args, **kwargs: calls.append(args))
    gold = [{"version": 2, "query": "q", "query_type": "fact", "answerable": True,
             "relevant": [{"doc_id": "a"}]}]
    with pytest.raises(ValueError):
        evaluation.evaluate(None, gold, configurations={"valid": {}, "invalid": bad})
    assert not calls
