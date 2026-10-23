"""Attachment import contracts, filesystem boundaries, preservation and recovery."""

import hashlib
import json
import os

import pytest
import yaml

from pkb import access, imports, mcp_server
from pkb.ingest import parse_frontmatter, process_file


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "study").mkdir(parents=True)
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    monkeypatch.setattr("pkb.config.settings.data_root", str(root))
    monkeypatch.setattr("pkb.config.settings.attachments_root", str(attachments))
    monkeypatch.setattr("pkb.config.settings.access_policy", "")
    indexed = []

    def ingest(files, base_dir, doc_id_prefix):
        chunks = process_file(files[0], base_dir, doc_id_prefix)
        indexed.append(chunks)
        return {"files": 1, "added": len(chunks)}

    monkeypatch.setattr(imports, "ingest_files", ingest)
    monkeypatch.setattr(imports, "document_status", lambda doc: imports.status_record(
        doc, "queued", len(indexed[-1]), len(indexed[-1]),
    ))
    return root, attachments, indexed


def _source(corpus, name="paper.txt", payload=b"Evidence about retrieval."):
    path = corpus[1] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return name, hashlib.sha256(payload).hexdigest()


IDENTITY = "12345678-1234-4234-8234-123456789abc"


def _import(path, sha, category="study", title="", *, document_id=IDENTITY, version=1):
    return imports.import_attachment(path, sha, category, title, document_id=document_id, version=version)


def test_text_import_preserves_original_and_indexes_policy_metadata(corpus):
    path, sha = _source(corpus)
    result = _import(path, sha, title="Research note")
    target = corpus[0] / "study" / "research" / f"import-{sha}.md"
    metadata, body = parse_frontmatter(target.read_text())
    assert result["doc_id"] == f"data/study/research/import-{sha}.md"
    assert result["attachment_path"] == path
    assert result["title"] == "Research note"
    assert result["extraction"] == result["indexing"] == "succeeded"
    assert result["graph"] == "queued"
    assert result["graph_status"]["pending_chunks"] == len(corpus[2][0])
    assert result["chars"] == len("Evidence about retrieval.")
    assert metadata["status"] == "draft"
    assert metadata["authority"] == "imported"
    assert metadata["source_type"] == "document"
    assert metadata["source"] == metadata["original_file"] == f"attachments/{path}"
    assert metadata["attachments"] == [f"attachments/{path}"]
    assert "Evidence about retrieval." in body
    assert (corpus[1] / path).read_bytes() == b"Evidence about retrieval."
    assert corpus[2][0][0]["status"] == "draft"
    assert corpus[2][0][0]["attachments"] == [f"attachments/{path}"]
    assert "검토 전 초안" in result["message"]
    assert corpus[2][0][0]["import_extraction"] == "succeeded"


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".markdown"])
def test_supported_formats_use_existing_converter(corpus, monkeypatch, suffix):
    path, sha = _source(corpus, "source" + suffix)
    observed = []

    def convert(snapshot):
        observed.append(snapshot)
        assert snapshot.suffix == suffix
        assert snapshot.read_bytes() == (corpus[1] / path).read_bytes()
        return "Extracted source text."

    monkeypatch.setattr(imports, "read_file_as_text", convert)
    assert _import(path, sha)["extraction"] == "succeeded"
    assert len(observed) == 1 and not observed[0].exists()


@pytest.mark.parametrize("name", ["photo.jpg", "audio.m4a", "arbitrary.bin"])
def test_unsupported_creates_searchable_metadata_only_card(corpus, monkeypatch, name):
    path, sha = _source(corpus, name, b"not readable content")
    monkeypatch.setattr(imports, "read_file_as_text", lambda _: pytest.fail("unsupported conversion"))
    result = _import(path, sha)
    assert result["extraction"] == "unsupported" and result["chars"] == 0
    assert result["indexing"] == "succeeded" and corpus[2][0]
    assert result["graph"] == "not_applicable"
    assert corpus[2][0][0]["import_extraction"] == "unsupported"
    assert "메타데이터만" in result["message"]


def test_empty_text_is_explicit_metadata_only_success(corpus, monkeypatch):
    path, sha = _source(corpus, "scan.pdf")
    monkeypatch.setattr(imports, "read_file_as_text", lambda _: "")
    result = _import(path, sha)
    assert result["extraction"] == "empty" and result["chars"] == 0
    assert result["indexing"] == "succeeded"
    assert "메타데이터만" in result["message"]
    assert result["graph"] == "not_applicable"


def test_graph_outage_does_not_fail_or_erase_successful_import(corpus, monkeypatch):
    path, sha = _source(corpus)

    def unavailable(_):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(imports, "document_status", unavailable)
    result = _import(path, sha)
    assert result["indexing"] == "succeeded" and result["graph"] == "waiting"
    assert result["graph_status"]["pending_chunks"] == len(corpus[2][0])
    assert (corpus[0] / result["doc_id"].removeprefix("data/")).is_file()


def test_retry_reindexes_exact_record_without_reconversion_or_title_change(corpus, monkeypatch):
    path, sha = _source(corpus)
    first = _import(path, sha, title="Original title")
    target = corpus[0] / first["doc_id"].removeprefix("data/")
    before = target.read_bytes(), target.stat().st_mtime_ns
    monkeypatch.setattr(imports, "read_file_as_text", lambda _: pytest.fail("retry conversion"))
    retry = _import(path, sha, title="New title")
    assert retry == first
    assert before == (target.read_bytes(), target.stat().st_mtime_ns)
    assert len(corpus[2]) == 2


@pytest.mark.parametrize("change", ["append", "metadata", "replace"])
def test_edited_document_is_not_overwritten_or_reindexed(corpus, change):
    path, sha = _source(corpus)
    result = _import(path, sha)
    target = corpus[0] / result["doc_id"].removeprefix("data/")
    original = target.read_text()
    edited = {"append": original + "Manual edit", "metadata": original.replace("draft", "active"),
              "replace": "Handwritten document"}[change]
    target.write_text(edited)
    with pytest.raises(ValueError, match="수정되었습니다"):
        _import(path, sha)
    assert target.read_text() == edited
    assert len(corpus[2]) == 1


def test_index_failure_leaves_recoverable_document_and_reports_stage(corpus, monkeypatch):
    path, sha = _source(corpus)
    real_ingest = imports.ingest_files
    monkeypatch.setattr(imports, "ingest_files", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    tool = getattr(mcp_server.import_attachment, "fn", mcp_server.import_attachment)
    failure = tool(path, sha, "study", document_id=IDENTITY, version=1)
    assert failure.startswith("오류:") and "indexing 단계 실패" in failure
    assert (corpus[0] / "study" / "research" / f"import-{sha}.md").is_file()
    monkeypatch.setattr(imports, "ingest_files", real_ingest)
    assert json.loads(tool(path, sha, "study", document_id=IDENTITY, version=1))["indexing"] == "succeeded"


def test_zero_indexed_files_is_not_reported_as_success(corpus, monkeypatch):
    path, sha = _source(corpus)
    monkeypatch.setattr(imports, "ingest_files", lambda *a, **kw: {"files": 0, "added": 0})
    with pytest.raises(ValueError, match="indexing 단계 실패"):
        _import(path, sha)


def test_conversion_failure_does_not_create_a_document(corpus, monkeypatch):
    path, sha = _source(corpus, "broken.pdf")

    def fail(_):
        raise ValueError("corrupt document")

    monkeypatch.setattr(imports, "read_file_as_text", fail)
    with pytest.raises(ValueError, match="extraction 단계 실패"):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))
    assert (corpus[1] / path).read_bytes() == b"Evidence about retrieval."


def test_extracted_text_limit_is_enforced_before_creating_document(corpus, monkeypatch):
    path, sha = _source(corpus, "expanded.docx")
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 1000)
    monkeypatch.setattr(imports, "read_file_as_text", lambda _: "x" * 1001)
    with pytest.raises(ValueError, match="추출한 텍스트가 50 MiB"):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


def test_atomic_creation_preserves_a_concurrent_writer(corpus, monkeypatch):
    path, sha = _source(corpus)
    real_link = os.link

    def race(source, destination):
        destination.write_text("Concurrent handwritten document")
        real_link(source, destination)

    monkeypatch.setattr(imports.os, "link", race)
    with pytest.raises(ValueError, match="수정되었습니다"):
        _import(path, sha)
    target = corpus[0] / "study" / "research" / f"import-{sha}.md"
    assert target.read_text() == "Concurrent handwritten document"
    assert not list(target.parent.glob(".pkb-import-*"))
    assert corpus[2] == []


def test_same_bytes_at_another_path_do_not_replace_existing_provenance(corpus):
    path, sha = _source(corpus)
    _import(path, sha)
    other, same_sha = _source(corpus, "other.txt")
    with pytest.raises(ValueError, match="원본 정보가 다릅니다"):
        _import(other, same_sha)
    assert len(corpus[2]) == 1


@pytest.mark.parametrize("path", ["../paper.txt", "/paper.txt", "a/../paper.txt", ".hidden/paper", "a\\b", "a\0b"])
def test_path_traversal_is_rejected_without_creating_records(corpus, path):
    _, sha = _source(corpus)
    with pytest.raises(ValueError):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


@pytest.mark.parametrize("category", ["", "../outside", "missing", "study/research", "_origin", "research", ".hidden"])
def test_only_existing_top_level_category_is_allowed(corpus, category):
    path, sha = _source(corpus)
    with pytest.raises(ValueError):
        _import(path, sha, category)
    assert not list(corpus[0].rglob("*.md"))


def test_hash_mismatch_and_invalid_hash_do_not_create_document(corpus):
    path, _ = _source(corpus)
    for sha in ("0" * 64, "not a sha"):
        with pytest.raises(ValueError, match="sha256"):
            _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


@pytest.mark.parametrize("location", ["file", "source_parent", "destination"])
def test_symlinks_are_rejected_even_when_they_point_inside_roots(corpus, monkeypatch, location):
    path, sha = _source(corpus)
    if location == "file":
        link = corpus[1] / "link.txt"
        link.symlink_to(corpus[1] / path)
        path = link.name
    elif location == "source_parent":
        (corpus[1] / "link").symlink_to(corpus[1], target_is_directory=True)
        path = "link/" + path
    else:
        (corpus[0] / "study" / "research").symlink_to(corpus[0] / "study", target_is_directory=True)
    with pytest.raises(ValueError, match="심볼릭"):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


def test_nonregular_and_over_attachment_limit_sources_are_rejected(corpus, monkeypatch):
    fifo = corpus[1] / "pipe.txt"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="일반 첨부"):
        _import(fifo.name, "0" * 64)
    path, sha = _source(corpus)
    monkeypatch.setattr(imports, "MAX_ATTACHMENT_BYTES", 3)
    with pytest.raises(ValueError, match="2 GiB"):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


@pytest.mark.parametrize("extension", [".m4a", ".pdf"])
def test_over_50_mib_original_is_stream_hashed_without_snapshot_or_conversion(corpus, monkeypatch, extension):
    source = corpus[1] / ("large" + extension)
    with source.open("wb") as stream:
        stream.truncate(imports.MAX_IMPORT_BYTES + 1)
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()

    def no_snapshot(*args, **kwargs):
        pytest.fail("metadata-only originals must not be copied or converted")

    monkeypatch.setattr(imports.tempfile, "TemporaryDirectory", no_snapshot)
    monkeypatch.setattr(imports, "read_file_as_text", no_snapshot)
    result = _import(source.name, sha)
    assert result["extraction"] == "unsupported" and result["indexing"] == "succeeded"
    assert result["chars"] == 0
    assert source.stat().st_size == imports.MAX_IMPORT_BYTES + 1
    if extension == ".pdf":
        assert "50 MiB" in result["message"]
    assert _import(source.name, sha) == result


@pytest.mark.parametrize("which", ["data", "attachments"])
def test_trusted_configured_root_alias_is_allowed(corpus, monkeypatch, which):
    path, sha = _source(corpus)
    root = corpus[0] if which == "data" else corpus[1]
    alias = root.parent / (which + "-alias")
    alias.symlink_to(root, target_is_directory=True)
    setting = "data_root" if which == "data" else "attachments_root"
    monkeypatch.setattr("pkb.config.settings." + setting, str(alias))
    assert _import(path, sha)["indexing"] == "succeeded"


@pytest.mark.parametrize("denied", ["attachments", "destination"])
def test_both_attachment_read_and_destination_write_are_required(corpus, monkeypatch, denied):
    path, sha = _source(corpus)
    policy = {
        "version": 1, "default": {}, "principals": {"reader": {}},
        "rules": [
            {"path": "attachments", "read": ["user:reader"] if denied != "attachments" else []},
            {"path": "data/study", "write": ["user:reader"] if denied != "destination" else []},
        ],
    }
    policy_path = corpus[0].parent / "policy.yml"
    policy_path.write_text(yaml.safe_dump(policy))
    monkeypatch.setattr("pkb.config.settings.access_policy", str(policy_path))
    with access.acting_as(access.load_policy().principal("reader")), pytest.raises(ValueError, match="접근"):
        _import(path, sha)
    assert not list(corpus[0].rglob("*.md"))


def test_import_is_registered_in_core_profile():
    assert "import_attachment" in mcp_server.CORE_TOOLS
    assert mcp_server.mcp._tool_manager._tools["import_attachment"].fn.pkb_access == "write"


def test_identity_validation_and_immutable_version_conflicts(corpus):
    path, sha = _source(corpus)
    for identity, version in ((IDENTITY.upper(), 1), ("note", 1), (IDENTITY, 0), (IDENTITY, True)):
        with pytest.raises(ValueError):
            _import(path, sha, document_id=identity, version=version)
    first = _import(path, sha)
    before = (corpus[0] / first["doc_id"].removeprefix("data/")).read_bytes()
    with pytest.raises(ValueError, match="문서ID·버전"):
        _import(path, sha, version=2)
    other, other_sha = _source(corpus, "other.txt", b"Changed evidence")
    with pytest.raises(ValueError, match="문서ID·버전"):
        _import(other, other_sha)
    assert (corpus[0] / first["doc_id"].removeprefix("data/")).read_bytes() == before
    assert len(corpus[2]) == 1


def test_out_of_order_retry_keeps_old_version_and_metadata(corpus):
    path, sha = _source(corpus)
    first = _import(path, sha, title="Version one")
    other, other_sha = _source(corpus, "v2.txt", b"Second revision")
    second = _import(other, other_sha, title="Version two", version=2)
    assert _import(path, sha, title="Ignored retry title") == first
    assert first["document_id"] == second["document_id"] == IDENTITY
    assert (first["version"], second["version"]) == (1, 2)
    assert corpus[2][-1][0]["import_version"] == 1
    assert corpus[2][-1][0]["title"] == "Version one"


def _legacy_card(corpus, name="legacy.txt", payload=b"Legacy preserved evidence", version=1):
    path, sha = _source(corpus, name, payload)
    result = _import(path, sha, version=version)
    target = corpus[0] / result["doc_id"].removeprefix("data/")
    unsigned = target.read_text().split("\n<!-- pkb-import-record-sha256:")[0]
    unsigned = "\n".join(line for line in unsigned.split("\n")
                         if not line.startswith(("import_document_id:", "import_version:")))
    digest = hashlib.sha256(unsigned.encode()).hexdigest()
    target.write_text(unsigned + f"\n<!-- pkb-import-record-sha256: {digest} -->\n")
    return {"doc_id": result["doc_id"], "document_id": IDENTITY, "version": version}, target, path, sha


def test_legacy_retry_requires_explicit_migration(corpus):
    _, target, path, sha = _legacy_card(corpus)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="마이그레이션"):
        _import(path, sha)
    assert target.read_bytes() == before


def test_migration_preflights_whole_plan_and_dry_run_writes_nothing(corpus, monkeypatch):
    from pkb.import_versions import migrate_import_versions

    first, target, path, _ = _legacy_card(corpus)
    second, other, other_path, _ = _legacy_card(corpus, "other.txt", b"Other legacy", version=2)
    originals = target.read_bytes(), other.read_bytes()
    monkeypatch.setattr("pkb.import_versions.ingest_files", lambda *a, **kw: pytest.fail("dry run indexed"))
    plan = migrate_import_versions([first, second])
    assert plan["dry_run"] and len(plan["documents"]) == 2
    assert all(item["metadata_changed"] for item in plan["documents"])
    assert not (corpus[0] / "_archive" / "import-versions").exists()
    assert originals == (target.read_bytes(), other.read_bytes())
    (corpus[1] / other_path).write_bytes(b"Changed original")
    with pytest.raises(ValueError, match="sha256"):
        migrate_import_versions([first, second], dry_run=False)
    assert originals == (target.read_bytes(), other.read_bytes())
    assert not (corpus[0] / "_archive" / "import-versions").exists()
    assert (corpus[1] / path).read_bytes() == b"Legacy preserved evidence"


def test_migration_rejects_duplicate_slots_and_invalid_records(corpus):
    from pkb.import_versions import migrate_import_versions

    first, target, _, _ = _legacy_card(corpus)
    second, other, _, _ = _legacy_card(corpus, "other.txt", b"Other legacy", version=2)
    with pytest.raises(ValueError, match="문서ID·버전"):
        migrate_import_versions([first, {**second, "version": 1}], dry_run=False)
    other.write_bytes(other.read_bytes() + b"changed")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="수정되었습니다"):
        migrate_import_versions([first, second], dry_run=False)
    assert target.read_bytes() == before
    assert not (corpus[0] / "_archive" / "import-versions").exists()


def test_migration_backup_preservation_resume_and_full_ingest_idempotency(corpus, monkeypatch):
    from unittest.mock import MagicMock

    from pkb import import_versions, store
    from pkb.ingest import ingest_files

    assignment, target, path, sha = _legacy_card(corpus)
    raw = target.read_bytes()
    old_body = parse_frontmatter(raw.decode())[1].split("\n<!-- pkb-import-record-sha256:")[0]
    saved = {c["chunk_index"]: c for c in process_file(target, corpus[0], "data/")}
    monkeypatch.setattr(store, "get_client", MagicMock)
    monkeypatch.setattr(store, "ensure_item_mapping", lambda es: None)
    monkeypatch.setattr(store, "get_existing_chunks", lambda es, doc: saved.copy())
    monkeypatch.setattr(store, "refresh_document_rows", lambda *a: 1)
    monkeypatch.setattr("pkb.config.settings.graph_db_path", str(corpus[0] / "missing.sqlite"))
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: [[1.0] for _ in texts])

    def apply(es, doc, new_chunks, metadata_updates, delete_indices):
        for chunk in new_chunks:
            saved[chunk["chunk_index"]] = chunk.copy()
        for index, delta in metadata_updates:
            saved[index].update(delta)
        for index in delete_indices:
            del saved[index]

    monkeypatch.setattr(store, "apply_chunk_delta", apply)
    def unavailable(*a, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(import_versions, "ingest_files", unavailable)
    with pytest.raises(RuntimeError, match="offline"):
        import_versions.migrate_import_versions([assignment], dry_run=False)
    migrated = target.read_bytes()
    backup = corpus[0] / "_archive" / "import-versions" / assignment["doc_id"].removeprefix("data/")
    assert backup.read_bytes() == raw
    assert migrated != raw
    assert parse_frontmatter(migrated.decode())[1].split("\n<!-- pkb-import-record-sha256:")[0] == old_body
    monkeypatch.setattr(import_versions, "ingest_files", ingest_files)
    result = import_versions.migrate_import_versions([assignment], dry_run=False)
    assert result["documents"][0]["metadata_changed"] is False
    assert all(c["import_document_id"] == IDENTITY and c["import_version"] == 1 for c in saved.values())
    assert imports._load_record(target, "attachments/" + path, sha)[1]["import_version"] == 1
    monkeypatch.setattr("pkb.embeddings.embed_documents", lambda texts: pytest.fail("repeat reembedded"))
    repeat = ingest_files([target], base_dir=corpus[0], doc_id_prefix="data/")
    assert repeat["metadata_updated"] == repeat["embedded"] == repeat["added"] == 0
    assert repeat["reused"] == len(saved)
    assert target.read_bytes() == migrated and backup.read_bytes() == raw
    assert (corpus[1] / path).read_bytes() == b"Legacy preserved evidence"
    assert import_versions.migrate_import_versions([assignment], dry_run=False)["indexing"]["reused"] == len(saved)
    # Backups must be excluded by the existing corpus policy rules. Exercise the
    # public import path so canonical-id validation also sees the backup file.
    monkeypatch.setattr(imports, "ingest_files", ingest_files)
    retry = imports.import_attachment(path, sha, "study", "Ignored retry title", document_id=IDENTITY, version=1)
    assert retry["indexing"] == "succeeded"
    assert retry["document_id"] == IDENTITY and retry["version"] == 1
    assert retry["doc_id"] == assignment["doc_id"]
    assert target.read_bytes() == migrated and backup.read_bytes() == raw


def test_import_and_migration_reject_cross_category_lineage(corpus):
    from pkb.import_versions import migrate_import_versions

    (corpus[0] / "other").mkdir()
    path, sha = _source(corpus)
    _import(path, sha)
    other, other_sha = _source(corpus, "other.txt", b"Other version")
    with pytest.raises(ValueError, match="카테고리"):
        _import(other, other_sha, category="other", version=2)
    other_identity = "12345678-1234-4234-8234-123456789abd"
    result = _import(other, other_sha, category="other", document_id=other_identity)
    target = corpus[0] / result["doc_id"].removeprefix("data/")
    unsigned = target.read_text().split("\n<!-- pkb-import-record-sha256:")[0]
    unsigned = "\n".join(line for line in unsigned.split("\n")
                         if not line.startswith(("import_document_id:", "import_version:")))
    digest = hashlib.sha256(unsigned.encode()).hexdigest()
    target.write_text(unsigned + f"\n<!-- pkb-import-record-sha256: {digest} -->\n")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="카테고리"):
        migrate_import_versions([{"doc_id": result["doc_id"], "document_id": IDENTITY, "version": 2}], dry_run=False)
    assert target.read_bytes() == before
    assert not (corpus[0] / "_archive" / "import-versions").exists()
