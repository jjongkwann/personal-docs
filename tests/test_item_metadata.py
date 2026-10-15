"""수집 항목 frontmatter(source_type·occurred_at·people·links…)의 색인 전파와 매핑."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from pkb import store
from pkb.ingest import _diff_metadata, process_file
from pkb.items import ITEM_METADATA_FIELDS, normalize_item_metadata, normalize_person
from pkb.operations import frontmatter_warnings
from pkb.store import DOC_INDEX_SETTINGS, INDEX_SETTINGS, ensure_item_mapping

BODY = "A sufficiently long body keeps this as a normal searchable chunk for the meeting.\n"


def _note(tmp_path, frontmatter: str):
    root = tmp_path / "data"
    note = root / "meetings" / "2026" / "standup.md"
    note.parent.mkdir(parents=True)
    note.write_text(f"---\n{frontmatter}---\n\n{BODY}", encoding="utf-8")
    return note, root


def test_process_file_puts_item_fields_on_every_chunk(tmp_path):
    note, root = _note(
        tmp_path,
        "source_type: Meeting\n"
        "occurred_at: 2026-10-15T10:00:00+09:00\n"
        "people: [Alice@Example.COM, 김철수, alice@example.com]\n"
        "source_id: evt-123\n"
        "source_link: https://calendar.example/evt-123\n"
        "links: data/calendar/2026/evt-123.md, data/mail/2026/10/thread.md\n"
        "attachments: [meetings/2026/standup.m4a]\n",
    )

    chunks = process_file(note, root)

    assert chunks
    for chunk in chunks:
        assert chunk["source_type"] == "meeting"
        assert chunk["occurred_at"] == "2026-10-15T10:00:00+09:00"
        assert chunk["people"] == ["alice@example.com", "김철수"]
        assert chunk["source_id"] == "evt-123"
        assert chunk["source_link"] == "https://calendar.example/evt-123"
        assert chunk["links"] == ["data/calendar/2026/evt-123.md", "data/mail/2026/10/thread.md"]
        assert chunk["attachments"] == ["meetings/2026/standup.m4a"]


def test_legacy_note_has_no_item_fields(tmp_path):
    note, root = _note(tmp_path, "title: 옛 노트\n")
    chunk = process_file(note, root)[0]
    assert not set(ITEM_METADATA_FIELDS) & set(chunk)


def test_occurred_at_accepts_yaml_dates_and_drops_invalid_values():
    assert normalize_item_metadata({"occurred_at": date(2026, 10, 15)}) == {"occurred_at": "2026-10-15"}
    moment = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    assert normalize_item_metadata({"occurred_at": moment}) == {"occurred_at": "2026-10-15T01:00:00+00:00"}
    assert normalize_item_metadata({"occurred_at": "어제"}) == {}
    assert normalize_item_metadata({"occurred_at": None}) == {"occurred_at": None}


def test_person_normalization_lowercases_only_email_addresses():
    assert normalize_person(" Bob@Example.com ") == "bob@example.com"
    assert normalize_person("Bob Smith") == "Bob Smith"


def test_item_list_fields_compare_as_sets_and_absent_keys_do_not_clear():
    old = {"people": ["b@x.com", "a@x.com"], "source_type": "meeting"}
    assert _diff_metadata(old, {"people": ["a@x.com", "b@x.com"], "source_type": "meeting"}) == {}
    assert not {"people", "source_type"} & set(_diff_metadata(old, {"title": "legacy"}))
    assert _diff_metadata(old, {"people": None, "source_type": "meeting"}) == {"people": None}


def test_invalid_occurred_at_is_a_write_warning():
    warnings = frontmatter_warnings("---\ntitle: x\noccurred_at: 어제\n---\nbody\n")
    assert any("occurred_at" in warning for warning in warnings)
    assert frontmatter_warnings("---\ntitle: x\noccurred_at: 2026-10-15\n---\nbody\n") == ()


def test_mappings_index_item_fields_with_unsearched_source_link():
    for settings_ in (INDEX_SETTINGS, DOC_INDEX_SETTINGS):
        props = settings_["mappings"]["properties"]
        assert props["occurred_at"]["type"] == "date"
        for field in ("source_type", "people", "source_id", "links", "attachments"):
            assert props[field]["type"] == "keyword"
        assert props["source_link"] == {"type": "keyword", "index": False}


def test_existing_index_gets_item_fields_once_per_process(monkeypatch):
    monkeypatch.setattr(store, "_item_mapping_checked", set())
    es = MagicMock()
    es.indices.exists.return_value = True

    ensure_item_mapping(es)
    ensure_item_mapping(es)

    es.indices.put_mapping.assert_called_once()
    properties = es.indices.put_mapping.call_args.kwargs["properties"]
    assert set(properties) == set(ITEM_METADATA_FIELDS)


def test_missing_index_is_left_for_create_index(monkeypatch):
    monkeypatch.setattr(store, "_item_mapping_checked", set())
    es = MagicMock()
    es.indices.exists.return_value = False
    ensure_item_mapping(es)
    es.indices.put_mapping.assert_not_called()
