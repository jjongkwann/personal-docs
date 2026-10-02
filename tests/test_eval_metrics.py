"""Multi-relevance retrieval metrics and explicit legacy migration."""
import json

import pytest

from pkb.eval import doc_ranking, eval_query, load_gold, migrate_legacy_gold, mrr


def _row(relevant, *, answerable=True):
    return {"version": 2, "query": "q", "query_type": "fact", "answerable": answerable, "relevant": relevant}


def test_document_rank_multi_recall_ndcg_and_exact_evidence():
    row = _row([
        {"doc_id": "a", "chunk_index": 2, "relevance": 2},
        {"canonical_id": "b", "relevance": 1},
    ])
    hits = [
        {"doc_id": "wrong", "chunk_index": 0},
        {"doc_id": "a", "chunk_index": 0},
        {"doc_id": "a", "chunk_index": 2},
        {"doc_id": "physical-b", "canonical_id": "b", "chunk_index": 1},
    ]
    assert doc_ranking(hits) == ["wrong", "a", "physical-b"]
    result = eval_query(row, hits, 4)
    assert result["rank"] == 2
    assert result["recall_at_k"] == 1
    assert result["evidence_recall_at_k"] == 1
    assert 0 < result["ndcg_at_k"] < 1
    assert eval_query(row, hits, 2)["recall_at_k"] == 0.5
    assert eval_query(row, hits, 2)["evidence_recall_at_k"] == 0


def test_noanswer_reports_false_positive_not_reciprocal_rank():
    row = _row([], answerable=False)
    assert eval_query(row, [], 5)["noanswer_false_positive"] is False
    assert eval_query(row, [{"doc_id": "a"}], 5)["noanswer_false_positive"] is True
    assert eval_query(row, [{"doc_id": "a"}], 5)["rank"] is None


def test_same_canonical_result_does_not_double_credit_ndcg():
    row = _row([{"canonical_id": "one", "relevance": 2}])
    hits = [{"doc_id": "a", "canonical_id": "one"}, {"doc_id": "b", "canonical_id": "one"}]
    assert eval_query(row, hits, 5)["ndcg_at_k"] == 1


def test_multi_evidence_one_document_counts_once_for_document_metrics(tmp_path):
    row = _row([{"doc_id": "a", "chunk_index": 1, "relevance": 2},
                {"doc_id": "a", "chunk_index": 2, "relevance": 1}])
    path = tmp_path / "gold.jsonl"
    path.write_text(json.dumps(row), encoding="utf-8")
    assert load_gold(path) == [row]
    result = eval_query(row, [{"doc_id": "a", "chunk_index": 1}], 5)
    assert result["rank"] == 1
    assert result["recall_at_k"] == 1
    assert result["ndcg_at_k"] == 1
    assert result["evidence_recall_at_k"] == 0.5


def test_first_chunk_wins_for_document_metadata():
    row = _row([{"canonical_id": "right"}])
    hits = [{"doc_id": "a", "canonical_id": "right"}, {"doc_id": "a", "canonical_id": "wrong"}]
    assert eval_query(row, hits, 2)["recall_at_k"] == 1


def test_mrr():
    assert mrr([1, 2, None]) == 0.5
    assert mrr([]) == 0


def test_versioned_gold_validation_and_explicit_migration(tmp_path):
    source = tmp_path / "old.jsonl"
    destination = tmp_path / "new.jsonl"
    source.write_text('{"query":"q","doc_id":"data/a.md"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="version 2"):
        load_gold(source)
    assert migrate_legacy_gold(source, destination) == 1
    assert load_gold(destination) == [_row([{"doc_id": "data/a.md"}]) | {"query_type": "legacy"}]
    assert json.loads(source.read_text(encoding="utf-8"))["doc_id"] == "data/a.md"
    with pytest.raises(FileExistsError):
        migrate_legacy_gold(source, destination)


def test_gold_rejects_duplicate_or_invalid_evidence(tmp_path):
    path = tmp_path / "gold.jsonl"
    row = _row([{"doc_id": "a", "chunk_index": -1}])
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="chunk_index"):
        load_gold(path)
    row["relevant"] = [{"canonical_id": "a", "chunk_index": 1}]
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="requires doc_id"):
        load_gold(path)
    row["relevant"] = [{"doc_id": "a", "chunk_index": 1}, {"doc_id": "a", "chunk_index": 1}]
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_gold(path)
