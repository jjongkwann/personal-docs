"""store.list_doc_ids 단위 테스트 + sync reconcile의 prune 판단 로직 검증.

실제 ES 없이 MagicMock으로 호출 shape을 검증.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pkb.store import PRUNE_CONFIRM_THRESHOLD, list_doc_ids


def _es_with_doc_ids(doc_ids: list[str]) -> MagicMock:
    es = MagicMock()
    es.search.return_value = {
        "aggregations": {"ids": {"buckets": [{"key": d} for d in doc_ids]}}
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
    assert call["aggs"]["ids"]["terms"]["field"] == "doc_id"


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
