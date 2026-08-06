"""Optional frontmatter document-contract metadata propagation tests."""

from __future__ import annotations

from pkb.ingest import _diff_metadata, process_file
from pkb.store import INDEX_SETTINGS


def test_process_file_propagates_document_contract_fields(tmp_path):
    root = tmp_path / "data"
    note = root / "study" / "contract.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\n"
        "schema_version: 1\n"
        "doc_type: canonical\n"
        "canonical_id: topic-1\n"
        "status: curated\n"
        "authority: primary\n"
        "aliases: [Topic One, topic-1, Topic One]\n"
        "source_ids: source-a\n"
        "supports: [claim-1]\n"
        "concept_ids: [c1, c2]\n"
        "---\n\n"
        "A sufficiently long body keeps this as a normal searchable chunk.\n",
        encoding="utf-8",
    )

    chunks = process_file(note, root)

    assert chunks
    metadata = chunks[0]
    assert metadata["schema_version"] == 1
    assert metadata["doc_type"] == "canonical"
    assert metadata["canonical_id"] == "topic-1"
    assert metadata["status"] == "curated"
    assert metadata["authority"] == "primary"
    assert metadata["aliases"] == ["Topic One", "topic-1", "Topic One"]
    assert metadata["source_ids"] == ["source-a"]
    assert metadata["supports"] == ["claim-1"]
    assert metadata["concept_ids"] == ["c1", "c2"]


def test_legacy_frontmatter_omits_optional_fields_and_does_not_clear_delta():
    chunks = _diff_metadata(
        {"canonical_id": "topic-1", "status": "curated"},
        {"title": "legacy"},
    )
    assert "canonical_id" not in chunks
    assert "status" not in chunks


def test_index_mapping_contains_document_contract_fields():
    props = INDEX_SETTINGS["mappings"]["properties"]
    for field in (
        "schema_version",
        "doc_type",
        "canonical_id",
        "status",
        "authority",
        "aliases",
        "source_ids",
        "supports",
        "concept_ids",
    ):
        assert props[field]["type"] == "keyword"
