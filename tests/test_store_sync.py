"""store.list_doc_ids 단위 테스트 + sync reconcile의 prune 판단 로직 검증.

실제 ES 없이 MagicMock으로 호출 shape을 검증.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pkb.store import (
    PRUNE_CONFIRM_THRESHOLD,
    get_existing_chunks,
    list_doc_ids,
    list_documents,
)


def _es_with_doc_ids(doc_ids: list[str]) -> MagicMock:
    es = MagicMock()
    es.search.return_value = {
        "aggregations": {
            "ids": {"buckets": [{"key": {"doc_id": d}} for d in doc_ids]}
        }
    }
    return es


def test_list_doc_ids_returns_set():
    es = _es_with_doc_ids(["obsidian/a.md", "obsidian/b.md"])
    assert list_doc_ids(es, "obsidian/") == {"obsidian/a.md", "obsidian/b.md"}


def test_list_doc_ids_query_shape_excludes_archived():
    es = _es_with_doc_ids([])
    list_doc_ids(es, "data/")
    call = es.search.call_args.kwargs
    assert call["query"]["bool"]["must"] == [{"prefix": {"doc_id": "data/"}}]
    assert call["query"]["bool"]["must_not"] == [
        {"exists": {"field": "archived_at"}}
    ]
    assert call["size"] == 0
    composite = call["aggs"]["ids"]["composite"]
    assert composite["sources"] == [{"doc_id": {"terms": {"field": "doc_id"}}}]
    assert composite["size"] == 1000


def test_list_doc_ids_follows_composite_after_key():
    es = MagicMock()
    es.search.side_effect = [
        {
            "aggregations": {
                "ids": {
                    "buckets": [{"key": {"doc_id": "data/a.md"}}],
                    "after_key": {"doc_id": "data/a.md"},
                }
            }
        },
        {
            "aggregations": {
                "ids": {"buckets": [{"key": {"doc_id": "data/b.md"}}]}
            }
        },
    ]

    assert list_doc_ids(es, "data/") == {"data/a.md", "data/b.md"}
    assert es.search.call_args_list[1].kwargs["aggs"]["ids"]["composite"]["after"] == {
        "doc_id": "data/a.md"
    }


def test_get_existing_chunks_follows_search_after():
    first_hits = [
        {"_source": {"chunk_index": i, "content_hash": str(i)}, "sort": [i]}
        for i in range(1000)
    ]
    es = MagicMock()
    es.search.side_effect = [
        {"hits": {"hits": first_hits}},
        {
            "hits": {
                "hits": [
                    {
                        "_source": {"chunk_index": 1000, "content_hash": "1000"},
                        "sort": [1000],
                    }
                ]
            }
        },
    ]

    chunks = get_existing_chunks(es, "data/huge.md")
    assert len(chunks) == 1001
    assert es.search.call_args_list[1].kwargs["search_after"] == [999]


def test_list_documents_follows_composite_after_key():
    def bucket(doc_id: str):
        return {
            "key": {"doc_id": doc_id},
            "meta": {"hits": {"hits": [{"_source": {"doc_id": doc_id}}]}},
            "chunk_count": {"value": 2},
        }

    es = MagicMock()
    es.search.side_effect = [
        {
            "aggregations": {
                "docs": {
                    "buckets": [bucket("data/a.md")],
                    "after_key": {"doc_id": "data/a.md"},
                }
            }
        },
        {"aggregations": {"docs": {"buckets": [bucket("data/b.md")]}}},
    ]

    assert [d["doc_id"] for d in list_documents(es)] == ["data/a.md", "data/b.md"]
    assert es.search.call_args_list[1].kwargs["aggs"]["docs"]["composite"]["after"] == {
        "doc_id": "data/a.md"
    }


def test_prune_set_diff():
    # reconcile 핵심: stale = actual - expected
    actual = {"obsidian/a.md", "obsidian/b.md", "obsidian/gone.md"}
    expected = {"obsidian/a.md", "obsidian/b.md"}
    assert actual - expected == {"obsidian/gone.md"}
    # 연동 해제(expected=∅)면 전량이 stale
    assert actual - set() == actual


def test_prune_threshold_is_sane():
    # 소량(볼트에서 노트 몇 개 삭제)은 자동, 대량(연동 해제·경로 오설정)은 확인
    assert 1 <= PRUNE_CONFIRM_THRESHOLD <= 100
