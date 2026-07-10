"""store.archive_document / restore_document / purge_archived 단위 테스트.

실제 ES 없이 MagicMock으로 호출 shape을 검증.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from pkb.store import archive_document, purge_archived, restore_document


def test_archive_sets_archived_at_and_reason():
    es = MagicMock()
    es.update_by_query.return_value = {"updated": 3}
    n = archive_document(es, "data/career/old_resume.md", reason="outdated")
    assert n == 3
    call = es.update_by_query.call_args.kwargs
    assert call["query"] == {"term": {"doc_id": "data/career/old_resume.md"}}
    script = call["script"]
    assert "ctx._source.archived_at = params.ts" in script["source"]
    assert "ctx._source.archive_reason = params.reason" in script["source"]
    assert script["lang"] == "painless"
    assert script["params"]["reason"] == "outdated"
    assert "ts" in script["params"]
    assert call["refresh"] is True


def test_archive_without_reason_omits_reason_field():
    es = MagicMock()
    es.update_by_query.return_value = {"updated": 0}
    archive_document(es, "doc1")
    script = es.update_by_query.call_args.kwargs["script"]
    assert "archive_reason" not in script["source"]
    assert "reason" not in script["params"]


def test_restore_removes_lifecycle_fields_only_when_archived():
    es = MagicMock()
    es.update_by_query.return_value = {"updated": 2}
    n = restore_document(es, "doc1")
    assert n == 2
    call = es.update_by_query.call_args.kwargs
    src = call["script"]["source"]
    assert "ctx._source.remove('archived_at')" in src
    assert "ctx._source.remove('archive_reason')" in src
    # 복원 대상은 archived_at이 있는 청크만
    must = call["query"]["bool"]["must"]
    assert {"term": {"doc_id": "doc1"}} in must
    assert {"exists": {"field": "archived_at"}} in must


def test_purge_without_before_deletes_all_archived():
    es = MagicMock()
    es.delete_by_query.return_value = {"deleted": 10}
    n = purge_archived(es)
    assert n == 10
    must = es.delete_by_query.call_args.kwargs["query"]["bool"]["must"]
    assert must == [{"exists": {"field": "archived_at"}}]


def test_purge_with_before_adds_range_clause():
    es = MagicMock()
    es.delete_by_query.return_value = {"deleted": 5}
    before = datetime(2025, 1, 1, tzinfo=UTC)
    purge_archived(es, before=before)
    must = es.delete_by_query.call_args.kwargs["query"]["bool"]["must"]
    assert {"exists": {"field": "archived_at"}} in must
    ranges = [c for c in must if "range" in c]
    assert ranges[0]["range"]["archived_at"]["lt"] == before.isoformat()
