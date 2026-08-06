"""Retrieval profile, canonical grouping, and boost primitives."""

from __future__ import annotations

import pytest

from pkb.retrieve import (
    _bm25_query,
    _cap_per_doc,
    _knn_query,
    apply_canonical_boost,
    canonical_group_key,
    cap_per_canonical,
    profile_filter,
)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("all", []),
        (
            "curated",
            [
                {"terms": {"doc_type": ["concept", "guide", "moc"]}},
                {"terms": {"status": ["canonical", "active"]}},
            ],
        ),
        (
            "evidence",
            [
                {"terms": {"doc_type": ["concept", "guide", "moc", "research"]}},
                {"terms": {"status": ["canonical", "active"]}},
            ],
        ),
        ("source", [{"term": {"doc_type": "source"}}]),
    ],
)
def test_profile_filter_shapes(profile, expected):
    assert profile_filter(profile) == expected


def test_profile_filter_rejects_unknown_name():
    with pytest.raises(ValueError):
        profile_filter("drafts")


def test_profile_filter_is_applied_to_bm25_and_knn():
    bm25 = _bm25_query("q", None, profile="curated")
    knn = _knn_query([0.0] * 4, k=5, category=None, profile="source")
    assert {"terms": {"status": ["canonical", "active"]}} in bm25["bool"]["filter"]
    assert {"term": {"doc_type": "source"}} in knn["filter"]


def test_canonical_group_caps_shared_logical_document():
    candidates = [
        {"doc_id": "source-a", "canonical_id": "topic-1", "chunk_index": 0},
        {"doc_id": "source-b", "canonical_id": "topic-1", "chunk_index": 1},
        {"doc_id": "source-c", "canonical_id": "topic-2", "chunk_index": 0},
    ]
    result = _cap_per_doc(candidates, top_k=2, max_per_doc=1, group_by_canonical=True)
    assert [r["doc_id"] for r in result] == ["source-a", "source-c"]
    assert canonical_group_key(candidates[0]) == "topic-1"
    assert canonical_group_key({"doc_id": "legacy"}) == "legacy"
    assert [r["doc_id"] for r in cap_per_canonical(candidates, top_k=2)] == [
        "source-a",
        "source-c",
    ]


def test_canonical_boost_reorders_only_metadata_bearing_hits():
    candidates = [
        {"doc_id": "legacy", "score": 1.0},
        {"doc_id": "canonical", "canonical_id": "topic-1", "score": 0.9},
    ]
    assert apply_canonical_boost(candidates, 0.2)[0]["doc_id"] == "canonical"
    assert candidates[0]["score"] == pytest.approx(1.08)
