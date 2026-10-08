"""Legal identities survive ingest, metadata deltas, and safe mapping migration."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
import yaml

from pkb.config import settings
from pkb.ingest import _diff_metadata, ingest_files, process_file
from pkb.legal import LEGAL_MAPPING_PROPERTIES, normalize_legal_metadata
from pkb.store import INDEX_SETTINGS, migrate_legal_mapping

STATUTE = {
    "legal_kind": "statute", "law_id": "fixture-law", "article_id": "제12조의2",
    "legal_version": "revision-1", "effective_from": "2024-01-01", "effective_to": "2025-01-01",
    "original_url": "https://example.org/law/revision-1", "original_location": "제12조의2 제1항",
}


def _note(tmp_path, name="revision-1.md", metadata=None, body="# 제12조의2\n근거 문단입니다."):
    path = tmp_path / name
    path.write_text("---\n" + yaml.safe_dump(metadata or STATUTE, allow_unicode=True) + "---\n" + body)
    return path


def test_normalize_preserves_identity_and_does_not_invent_unknown_dates():
    metadata = normalize_legal_metadata({
        "legal_kind": "judgment", "case_id": " 2024다1234 ", "decision_date": date(2024, 2, 29),
        "legal_version": None,
    })
    assert metadata == {
        "legal_kind": "judgment", "case_id": "2024다1234", "decision_date": "2024-02-29", "legal_version": None,
    }
    assert normalize_legal_metadata({"title": "legacy"}) == {}


def test_technical_source_provenance_does_not_require_legal_identity(tmp_path):
    metadata = {"original_url": "https://example.org/paper", "original_location": "Section 3"}
    note = _note(tmp_path, metadata=metadata, body="# Technical paper\nThe measured result.")
    chunk = process_file(note, tmp_path)[0]
    assert {key: chunk[key] for key in metadata} == metadata
    assert "legal_kind" not in chunk


@pytest.mark.parametrize("changes", [
    {"legal_kind": "law"}, {"law_id": None}, {"effective_from": "2024-02-30"},
    {"effective_from": "2024-1-1"}, {"effective_from": datetime(2024, 1, 1)},
    {"effective_to": "2024-01-01"}, {"effective_to": "2023-12-31"},
    {"effective_from": None}, {"article_id": ["제1조"]}, {"original_url": "file:///law.md"},
])
def test_invalid_legal_metadata_is_rejected(changes):
    with pytest.raises(ValueError):
        normalize_legal_metadata(STATUTE | changes)


def test_ingest_preserves_separate_versions_and_original_location(tmp_path):
    old = _note(tmp_path)
    current = _note(tmp_path, "revision-2.md", STATUTE | {
        "legal_version": "revision-2", "effective_from": "2025-01-01", "effective_to": None,
    })
    old_chunks = process_file(old, tmp_path)
    new_chunks = process_file(current, tmp_path)
    assert old_chunks[0]["doc_id"] != new_chunks[0]["doc_id"]
    for key, value in STATUTE.items():
        assert old_chunks[0][key] == value
    assert new_chunks[0]["effective_to"] is None
    assert old_chunks[0]["section_path"] == "제12조의2"
    assert old_chunks[0]["content_hash"] == new_chunks[0]["content_hash"]


def test_legal_only_delta_updates_metadata_without_embedding(tmp_path, monkeypatch):
    note = _note(tmp_path)
    old = process_file(note, tmp_path)[0]
    _note(tmp_path, metadata=STATUTE | {"original_location": "제12조의2 제2항", "effective_to": None})
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [{"_source": old}]}}
    es.bulk.return_value = {"errors": False}
    embed = MagicMock()
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed", embed)
    monkeypatch.setattr(settings, "graph_db_path", str(tmp_path / "absent.sqlite"))

    stats = ingest_files([note], tmp_path)

    embed.assert_not_called()
    assert stats["metadata_updated"] == 1
    ops = es.bulk.call_args.kwargs["operations"]
    assert ops[1]["doc"] == {"original_location": "제12조의2 제2항", "effective_to": None}
    assert "update" in ops[0]
    assert _diff_metadata(old, {key: value for key, value in old.items() if key not in STATUTE}) == {}


@pytest.mark.parametrize("moved", [False, True])
def test_replaced_and_moved_legacy_source_retains_stored_legal_metadata(tmp_path, monkeypatch, moved):
    monkeypatch.setattr(settings, "embed_context_prefix", False)
    note = _note(tmp_path)
    old = process_file(note, tmp_path)[0]
    body = "# Changed\nRevised content without frontmatter."
    note.write_text(body + ("\n" + old["content"] if moved else ""))
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [{"_source": old}]}}
    es.mget.return_value = {"docs": [{"found": True, "_source": {"chunk_index": 0, "embedding": [9.0]}}]}
    es.bulk.return_value = {"errors": False}
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.embeddings.embed", lambda texts: [[1.0] for _ in texts])
    monkeypatch.setattr(settings, "graph_db_path", str(tmp_path / "absent.sqlite"))

    ingest_files([note], tmp_path)

    ops = es.bulk.call_args.kwargs["operations"]
    indexed = [ops[i + 1] for i in range(0, len(ops), 2)]
    for chunk in indexed:
        for key, value in STATUTE.items():
            assert chunk[key] == value
    assert indexed[0]["embedding"] == [1.0]
    if moved:
        assert indexed[1]["embedding"] == [9.0]


def test_inherited_interval_is_validated_before_any_write(tmp_path, monkeypatch):
    note = _note(tmp_path)
    old = process_file(note, tmp_path)[0]
    updated = STATUTE | {"effective_from": "2026-01-01"}
    del updated["effective_to"]  # Inherited 2025 interval end would now be invalid.
    _note(tmp_path, metadata=updated)
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [{"_source": old}]}}
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    with pytest.raises(ValueError, match="exclusive"):
        ingest_files([note], tmp_path)
    es.bulk.assert_not_called()


def test_new_revision_cannot_overwrite_historical_document(tmp_path, monkeypatch):
    note = _note(tmp_path)
    old = process_file(note, tmp_path)[0]
    _note(tmp_path, metadata=STATUTE | {"legal_version": "revision-2"})
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": [{"_source": old}]}}
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    with pytest.raises(ValueError, match="separate source path"):
        ingest_files([note], tmp_path)
    es.bulk.assert_not_called()


def test_index_and_additive_migration_preserve_existing_mapping():
    for field, spec in LEGAL_MAPPING_PROPERTIES.items():
        assert INDEX_SETTINGS["mappings"]["properties"][field] == spec
    es = MagicMock()
    es.indices.exists_alias.return_value = False
    existing = {"content": {"type": "text"}, "law_id": {"type": "keyword"}}
    es.indices.get_mapping.return_value = {"isolated": {"mappings": {"properties": existing}}}

    added = migrate_legal_mapping(es, index="isolated")

    assert "law_id" not in added
    assert es.indices.put_mapping.call_args.kwargs == {
        "index": "isolated", "properties": {k: v for k, v in LEGAL_MAPPING_PROPERTIES.items() if k != "law_id"},
    }
    assert existing == {"content": {"type": "text"}, "law_id": {"type": "keyword"}}
    es.bulk.assert_not_called()
    es.indices.delete.assert_not_called()


def test_migration_refuses_alias_and_incompatible_mapping():
    es = MagicMock()
    es.indices.exists_alias.return_value = True
    with pytest.raises(ValueError, match="physical index"):
        migrate_legal_mapping(es, index="read-alias")
    es.indices.exists_alias.return_value = False
    es.indices.get_mapping.return_value = {"isolated": {"mappings": {"properties": {"law_id": {"type": "text"}}}}}
    with pytest.raises(ValueError, match="incompatible mapping"):
        migrate_legal_mapping(es, index="isolated")
    es.indices.put_mapping.assert_not_called()


def test_migration_is_idempotent_and_refuses_wildcards():
    es = MagicMock()
    es.indices.exists_alias.return_value = False
    es.indices.get_mapping.return_value = {"isolated": {"mappings": {"properties": LEGAL_MAPPING_PROPERTIES}}}
    assert migrate_legal_mapping(es, index="isolated") == []
    with pytest.raises(ValueError, match="one physical index"):
        migrate_legal_mapping(es, index="isolated*")
    es.indices.put_mapping.assert_not_called()
