"""문서 단위 색인: 행 구성, 동기화 지점, 목록·카테고리·관련 문서, alias 처리."""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pkb import mcp_server, store
from pkb.config import settings
from pkb.retrieve import related_documents

DOC = "data/meetings/2026/standup.md"


def _chunk(index: int, embedding: list[float] | None, **meta) -> dict:
    chunk = {"doc_id": DOC, "chunk_index": index, "category": "meetings", "title": "스탠드업", **meta}
    if embedding is not None:
        chunk["embedding"] = embedding
    return chunk


def _hits(sources: list[dict]) -> dict:
    return {"hits": {"hits": [{"_source": source, "sort": [i]} for i, source in enumerate(sources)]}}


def _es(doc_index: bool = True) -> MagicMock:
    es = MagicMock()
    es.indices.exists.return_value = doc_index
    es.bulk.return_value = {"errors": False}
    return es


# ---------- 행 구성 ----------

def test_row_takes_metadata_from_first_slot_and_normalizes_mean_vector():
    row = store.build_document_row([
        _chunk(1, [0.0, 4.0], people=["b@x.com"]),
        _chunk(0, [2.0, 0.0], people=["a@x.com"], source_type="meeting", occurred_at="2026-10-15", status=None),
    ])
    assert row["people"] == ["a@x.com"]
    assert row["source_type"] == "meeting"
    assert row["chunk_count"] == 2
    assert "status" not in row  # None은 행에 넣지 않는다
    assert "embedding" not in row and "chunk_index" not in row
    # 평균 (1, 2)의 방향
    assert row["doc_vector"] == pytest.approx([1 / math.sqrt(5), 2 / math.sqrt(5)])


def test_row_without_embeddings_has_no_vector():
    assert "doc_vector" not in store.build_document_row([_chunk(0, None)])


# ---------- 동기화 ----------

def test_refresh_is_noop_without_doc_index():
    es = _es(doc_index=False)
    assert store.refresh_document_rows(es, [DOC]) == 0
    es.search.assert_not_called()
    es.bulk.assert_not_called()


def test_refresh_indexes_present_docs_and_deletes_missing_ones():
    es = _es()
    es.search.side_effect = [_hits([_chunk(0, [1.0, 0.0])]), _hits([])]

    assert store.refresh_document_rows(es, [DOC, "data/gone.md", DOC]) == 1

    # 벡터를 읽어야 하므로 content만 뺀다
    assert es.search.call_args_list[0].kwargs["source_excludes"] == ["content"]
    ops = es.bulk.call_args.kwargs["operations"]
    assert ops[0] == {"index": {"_index": f"{settings.es_index}_docs", "_id": DOC}}
    assert ops[1]["doc_vector"] == [1.0, 0.0]
    assert ops[2] == {"delete": {"_index": f"{settings.es_index}_docs", "_id": "data/gone.md"}}


def test_refresh_raises_on_bulk_item_error():
    es = _es()
    es.search.return_value = _hits([_chunk(0, [1.0])])
    es.bulk.return_value = {"errors": True, "items": [{"index": {"error": {"type": "mapper_parsing_exception"}}}]}
    with pytest.raises(RuntimeError, match="mapper_parsing_exception"):
        store.refresh_document_rows(es, [DOC])


def test_delete_document_removes_the_row():
    es = _es()
    es.delete_by_query.return_value = {"deleted": 3}
    es.search.return_value = _hits([])
    store.delete_document(es, DOC)
    assert es.bulk.call_args.kwargs["operations"] == [
        {"delete": {"_index": f"{settings.es_index}_docs", "_id": DOC}}
    ]


def test_ingest_refreshes_only_documents_that_changed(monkeypatch):
    from pkb.ingest import _content_hash, embedding_fingerprint, ingest_files

    def chunk(doc_id, content):
        c = {"content": content, "content_hash": _content_hash(content), "source_path": doc_id,
             "category": "test", "doc_id": doc_id, "chunk_index": 0, "section_path": "", "title": "t",
             "tags": [], "date_modified": "2026-10-15", "language": "ko"}
        c["embedding_fingerprint"] = embedding_fingerprint(c)
        return c

    same, changed = chunk("data/same.md", "same"), chunk("data/changed.md", "new")
    stored = {"data/same.md": [same], "data/changed.md": [chunk("data/changed.md", "old")]}
    es = _es()
    es.search.side_effect = lambda **kw: _hits(
        [{k: v for k, v in c.items() if k != "content"} for c in stored[kw["query"]["term"]["doc_id"]]]
    )
    refreshed: list[list[str]] = []
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.store.refresh_document_rows", lambda es, ids: refreshed.append(list(ids)))
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(settings, "graph_db_path", "/nonexistent/graph.sqlite")
    by_name = {"same.md": [same], "changed.md": [changed]}
    monkeypatch.setattr("pkb.ingest.process_file", lambda path, *a, **kw: by_name[path.name])

    ingest_files([Path("same.md"), Path("changed.md")], Path("."))

    assert refreshed == [["data/changed.md"]]


# ---------- 읽기 ----------

def test_list_documents_reads_rows_in_the_same_shape():
    es = _es()
    es.search.return_value = {"hits": {"hits": [
        {"_source": {"doc_id": DOC, "category": "meetings", "title": "스탠드업", "chunk_count": 4}, "sort": [DOC]},
    ]}}

    docs = store.list_documents(es, "meetings")

    assert docs == [{"doc_id": DOC, "category": "meetings", "title": "스탠드업", "chunks": 4}]
    call = es.search.call_args.kwargs
    assert call["index"] == f"{settings.es_index}_docs"
    assert {"term": {"category": "meetings"}} not in call["query"]["bool"]["must"]
    assert "doc_vector" not in call["source_includes"]
    es.search.assert_called_once()  # 집계 없이 행 한 페이지


def test_list_categories_counts_documents_by_category():
    es = _es()
    es.search.return_value = _hits([
        {"doc_id": f"data/{category}/{i}.md", "category": category, "chunk_count": 1}
        for category, count in (("study", 7), ("meetings", 3)) for i in range(count)
    ])
    assert store.list_categories(es) == {"meetings": 3, "study": 7}
    call = es.search.call_args.kwargs
    assert call["query"] == {"bool": {"must": [
        {"bool": {"must_not": {"terms": {"source_type": ["image", "audio"]}}}},
        {"bool": {"must_not": {"exists": {"field": "archived_at"}}}},
    ]}}


def test_list_categories_requires_doc_index():
    with pytest.raises(ValueError, match="doc-index"):
        store.list_categories(_es(doc_index=False))


def test_related_documents_runs_knn_without_the_document_itself():
    es = _es()
    es.get.return_value = {"_source": {"doc_vector": [0.6, 0.8]}}
    es.search.return_value = {"hits": {"hits": [
        {"_source": {"doc_id": "data/other.md", "title": "다른 회의"}, "_score": 0.93},
    ]}}

    hits = related_documents(es, DOC, top_k=3, source_types=["meeting"])

    assert hits == [{"doc_id": "data/other.md", "title": "다른 회의", "score": 0.93}]
    knn = es.search.call_args.kwargs["knn"]
    assert knn["field"] == "doc_vector"
    assert knn["query_vector"] == [0.6, 0.8]
    assert knn["k"] == 3
    assert {"bool": {"must_not": {"term": {"doc_id": DOC}}}} in knn["filter"]
    assert {"terms": {"source_type": ["meeting"]}} in knn["filter"]
    assert {"bool": {"must_not": {"exists": {"field": "archived_at"}}}} in knn["filter"]
    assert es.search.call_args.kwargs["source_excludes"] == ["doc_vector"]


def test_related_documents_reports_unknown_document():
    from elasticsearch import NotFoundError

    es = _es()
    es.get.side_effect = NotFoundError("missing", MagicMock(status=404), {})
    with pytest.raises(ValueError, match="문서 색인에 없는"):
        related_documents(es, DOC)


# ---------- 색인 생성·alias ----------

def test_create_index_creates_doc_index_only_with_a_fresh_chunk_index(monkeypatch):
    monkeypatch.setattr(store, "_item_mapping_checked", set())
    fresh = MagicMock()
    fresh.indices.exists.return_value = False
    fresh.indices.exists_alias.return_value = False
    store.create_index(fresh)
    created = [call.kwargs["index"] for call in fresh.indices.create.call_args_list]
    assert created == [settings.es_index, f"{settings.es_index}_docs"]

    existing = MagicMock()
    existing.indices.exists.return_value = True
    store.create_index(existing)
    existing.indices.create.assert_not_called()
    assert existing.indices.put_mapping.call_count == 2
    assert existing.indices.put_mapping.call_args_list[1].kwargs["index"] == store.doc_index_name()


def test_doc_index_follows_the_physical_index_behind_an_alias(monkeypatch):
    monkeypatch.setattr(settings, "es_index", "pkb")
    es = MagicMock()
    es.indices.exists.return_value = False
    es.indices.exists_alias.return_value = True
    es.indices.get_alias.return_value = {"pkb_v2": {}}

    assert store.create_doc_index(es) is True

    assert es.indices.create.call_args.kwargs["index"] == "pkb_v2_docs"
    es.indices.put_alias.assert_called_once_with(index="pkb_v2_docs", name="pkb_docs")


@pytest.mark.parametrize("alias", [False, True])
def test_doc_index_uses_current_dimensions_after_model_selection(monkeypatch, alias):
    original_dims = store.DOC_INDEX_SETTINGS["mappings"]["properties"]["doc_vector"]["dims"]
    es = MagicMock()
    es.indices.exists.return_value = False
    es.indices.exists_alias.return_value = alias
    es.indices.get_alias.return_value = {"pkb_v2": {}}

    for dimensions in (original_dims + 1, original_dims + 2):
        monkeypatch.setattr(settings, "embedding_dims", dimensions)
        assert store.create_doc_index(es) is True

    mappings = [call.kwargs["body"] for call in es.indices.create.call_args_list]
    assert [body["mappings"]["properties"]["doc_vector"]["dims"] for body in mappings] == [
        original_dims + 1, original_dims + 2,
    ]
    assert store.DOC_INDEX_SETTINGS["mappings"]["properties"]["doc_vector"]["dims"] == original_dims


def test_switch_read_alias_moves_the_doc_alias_too(monkeypatch):
    monkeypatch.setattr(settings, "es_index", "pkb")
    es = MagicMock()
    es.indices.exists.return_value = True
    es.indices.exists_alias.return_value = True
    es.indices.get_alias.side_effect = lambda name: {"pkb": {"pkb_v1": {}}, "pkb_docs": {"pkb_v1_docs": {}}}[name]

    assert store.switch_read_alias(es, "pkb_v2") == ["pkb_v1"]

    assert es.indices.update_aliases.call_args.kwargs["actions"] == [
        {"remove": {"index": "pkb_v1", "alias": "pkb"}},
        {"remove": {"index": "pkb_v1_docs", "alias": "pkb_docs"}},
        {"add": {"index": "pkb_v2", "alias": "pkb"}},
        {"add": {"index": "pkb_v2_docs", "alias": "pkb_docs"}},
    ]


def test_switch_read_alias_requires_the_new_doc_index(monkeypatch):
    monkeypatch.setattr(settings, "es_index", "pkb")
    es = MagicMock()
    es.indices.exists.side_effect = lambda index: index != "pkb_v2_docs"
    with pytest.raises(ValueError, match="pkb doc-index"):
        store.switch_read_alias(es, "pkb_v2")
    es.indices.update_aliases.assert_not_called()


def test_backfill_writes_every_document_and_drops_orphan_rows(monkeypatch):
    es = _es()
    monkeypatch.setattr(store, "create_doc_index", lambda es: True)
    composite = {
        settings.es_index: [DOC, "data/b.md"],
        f"{settings.es_index}_docs": [DOC, "data/orphan.md"],
    }
    monkeypatch.setattr(
        store, "_composite_buckets",
        lambda es, name, query, index=None: [{"key": {"doc_id": d}} for d in composite[index or settings.es_index]],
    )
    refreshed: list[list[str]] = []
    monkeypatch.setattr(store, "refresh_document_rows", lambda es, ids: refreshed.append(list(ids)) or len(ids))

    assert store.backfill_document_index(es) == {"created": True, "documents": 2, "removed": 1}
    assert refreshed == [[DOC, "data/b.md"], ["data/orphan.md"]]


# ---------- MCP ----------

def test_mcp_list_categories_returns_json(monkeypatch):
    monkeypatch.setattr("pkb.store.get_client", lambda: None)
    monkeypatch.setattr("pkb.store.list_categories", lambda es, include_archived: {"a": 2, "b": 1})
    assert json.loads(mcp_server.list_categories()) == {"documents": 3, "categories": {"a": 2, "b": 1}}


def test_mcp_related_documents_returns_trimmed_json(monkeypatch):
    monkeypatch.setattr("pkb.store.get_client", lambda: None)
    monkeypatch.setattr(
        "pkb.retrieve.related_documents",
        lambda es, doc_id, **kw: [{"doc_id": "data/x.md", "title": "x", "tags": ["t"], "score": 0.9}],
    )
    payload = json.loads(mcp_server.related_documents(DOC))
    assert payload == {"doc_id": DOC, "related": [{"doc_id": "data/x.md", "title": "x", "score": 0.9}]}


def test_mcp_related_documents_reports_errors_as_text(monkeypatch):
    monkeypatch.setattr("pkb.store.get_client", lambda: None)
    assert mcp_server.related_documents(DOC, top_k=0).startswith("오류: ValueError")


def test_import_versions_choose_numeric_latest_and_count_visible_revisions():
    identity = "12345678-1234-4234-8234-123456789abc"
    es = _es()
    rows = [
        {"doc_id": "data/study/old.md", "category": "study", "chunk_count": 1,
         "import_document_id": identity, "import_version": 2, "date_modified": "2030-01-01"},
        {"doc_id": "data/study/new.md", "category": "study", "chunk_count": 1,
         "import_document_id": identity, "import_version": 10, "date_modified": "2020-01-01"},
        {"doc_id": "data/study/normal.md", "category": "study", "chunk_count": 1},
    ]
    es.search.side_effect = lambda **kw: _hits([row.copy() for row in rows])
    docs = store.list_documents(es)
    assert {doc["doc_id"] for doc in docs} == {"data/study/new.md", "data/study/normal.md"}
    assert next(doc for doc in docs if "import_version" in doc)["version_count"] == 2
    assert len(store.list_documents(es, include_versions=True)) == 3
    assert store.list_categories(es) == {"study": 2}


def test_latest_grouping_happens_after_acl_without_hidden_version_counts(tmp_path, monkeypatch):
    import yaml

    from pkb import access

    identity = "12345678-1234-4234-8234-123456789abc"
    policy = tmp_path / "policy.yml"
    policy.write_text(yaml.safe_dump({
        "version": 1, "default": {}, "principals": {"reader": {}},
        "rules": [{"path": "data/public", "read": ["user:reader"]}],
    }))
    monkeypatch.setattr(settings, "access_policy", str(policy))
    es = _es()
    rows = [
        {"doc_id": "data/public/v1.md", "category": "public", "chunk_count": 1,
         "import_document_id": identity, "import_version": 1},
        {"doc_id": "data/private/v2.md", "category": "private", "chunk_count": 1,
         "import_document_id": identity, "import_version": 2},
    ]
    es.search.side_effect = lambda **kw: _hits([row.copy() for row in rows])
    with access.acting_as(access.load_policy().principal("reader")):
        docs = store.list_documents(es)
        assert len(docs) == 1 and docs[0]["import_version"] == 1 and docs[0]["version_count"] == 1
        assert store.list_categories(es) == {"public": 1}
        assert len(store.list_documents(es, include_versions=True)) == 1
    assert "data/private" not in json.dumps(es.search.call_args.kwargs)


def test_latest_category_counts_and_filtered_lists_agree_after_category_change():
    identity = "12345678-1234-4234-8234-123456789abc"
    es = _es()
    rows = [
        {"doc_id": "data/old/v1.md", "category": "old", "chunk_count": 1,
         "import_document_id": identity, "import_version": 1},
        {"doc_id": "data/new/v2.md", "category": "new", "chunk_count": 1,
         "import_document_id": identity, "import_version": 2},
    ]
    es.search.side_effect = lambda **kw: _hits([row.copy() for row in rows])
    assert store.list_categories(es) == {"new": 1}
    assert store.list_documents(es, "old") == []
    assert len(store.list_documents(es, "new")) == 1
    assert store.list_documents(es, "new")[0]["version_count"] == 2
    assert len(store.list_documents(es, "old", include_versions=True)) == 1
