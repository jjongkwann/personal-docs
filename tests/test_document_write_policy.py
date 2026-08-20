"""Safe document write policy, previews, and optimistic locking."""

from __future__ import annotations

import pytest

from pkb.operations import (
    CanonicalIdConflictError,
    DocumentPolicyError,
    OptimisticLockError,
    content_hash,
    derive_document_type,
    resolve_document_policy,
    write_and_ingest,
)


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    root = tmp_path / "data"
    root.mkdir()
    return root


def _curated(doc_type: str = "concept", canonical_id: str = "c-1") -> str:
    return (
        "---\n"
        "schema_version: 1\n"
        "title: A title\n"
        f"doc_type: {doc_type}\n"
        f"canonical_id: {canonical_id}\n"
        "status: draft\n"
        "authority: curated\n"
        "tags: [pkb]\n"
        "---\n\nBody\n"
    )


def test_document_type_is_path_derived(data_root):
    assert derive_document_type("data/concepts/a.md") == "concept"
    assert derive_document_type("data/guides/a.md") == "guide"
    assert derive_document_type("data/research/a.md") == "research"
    assert derive_document_type("data/00_MOC.md") == "moc"
    assert derive_document_type("data/_origin/concepts/a.md") == "legacy"
    assert derive_document_type("data/writing/a.md") == "note"


def test_strict_policy_requires_curated_metadata_and_matching_type(data_root):
    with pytest.raises(DocumentPolicyError):
        write_and_ingest("data/guides/howto.md", "# no metadata", ingest=False, strict_policy=True)

    mismatched = _curated("research")
    with pytest.raises(DocumentPolicyError, match="doc_type"):
        write_and_ingest(
            "data/guides/howto.md", mismatched, ingest=False, strict_policy=True
        )


def test_legacy_path_keeps_warning_only_compatibility(data_root):
    result = write_and_ingest("data/writing/old.md", "# no metadata", ingest=False, strict_policy=True)
    assert result.changed
    assert (data_root / "writing" / "old.md").exists()
    assert result.warnings

    # Even a canonical-id diagnostic must not turn a legacy path into a hard
    # failure while strict curated writes are enabled globally.
    existing = data_root / "writing" / "other.md"
    existing.write_text("---\ncanonical_id: legacy\n---\n", encoding="utf-8")
    result = write_and_ingest(
        "data/writing/old2.md",
        "---\ncanonical_id: legacy\n---\n",
        ingest=False,
        strict_policy=True,
    )
    assert result.changed
    assert any("canonical_id" in warning for warning in result.warnings)


def test_canonical_id_conflict_is_detected_without_es(data_root):
    first = data_root / "guides" / "first.md"
    first.parent.mkdir(parents=True)
    first.write_text(_curated("guide", "same-id"), encoding="utf-8")

    policy = resolve_document_policy("data/guides/second.md", _curated("guide", "same-id"))
    assert policy.conflicts == (first.resolve(),)
    with pytest.raises(CanonicalIdConflictError):
        write_and_ingest(
            "data/guides/second.md",
            _curated("guide", "same-id"),
            ingest=False,
            strict_policy=True,
        )


def test_same_file_in_other_unicode_form_is_not_a_conflict(data_root):
    import unicodedata

    # 디스크에는 NFD 이름, 요청은 NFC 이름 — 같은 파일이 자기 자신과 충돌하면 안 된다
    nfd = unicodedata.normalize("NFD", "평가방법론.md")
    nfc = unicodedata.normalize("NFC", "평가방법론.md")
    target = data_root / "research" / nfd
    target.parent.mkdir(parents=True)
    target.write_text(_curated("research", "self-id"), encoding="utf-8")

    policy = resolve_document_policy(f"data/research/{nfc}", strict=True)
    assert policy.conflicts == ()


def test_dry_run_returns_diff_without_creating_or_ingesting(data_root, monkeypatch):
    def fail_ingest(*args, **kwargs):  # pragma: no cover - should never execute
        raise AssertionError("dry-run must not ingest")

    monkeypatch.setattr("pkb.ingest.ingest_files", fail_ingest)
    result = write_and_ingest(
        "data/research/draft.md",
        _curated("research"),
        ingest=True,
        dry_run=True,
        strict_policy=True,
    )
    assert result.dry_run is True
    assert result.changed is True
    assert result.diff
    assert result.content_hash == content_hash(_curated("research"))
    assert not (data_root / "research" / "draft.md").exists()


def test_expected_hash_protects_existing_file(data_root):
    body = "# existing\n"
    result = write_and_ingest("data/writing/existing.md", body, ingest=False)
    assert result.previous_hash is None
    assert result.content_hash == content_hash(body)

    with pytest.raises(OptimisticLockError):
        write_and_ingest(
            "data/writing/existing.md",
            "# changed\n",
            ingest=False,
            expected_hash="0" * 64,
        )

    updated = write_and_ingest(
        "data/writing/existing.md",
        "# changed\n",
        ingest=False,
        expected_hash=content_hash(body),
    )
    assert updated.previous_hash == content_hash(body)
    assert updated.content_hash == content_hash("# changed\n")
