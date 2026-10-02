"""빈 원본만 기존 색인과 그래프 파생 데이터를 정리한다."""

from unittest.mock import MagicMock

import pytest

from pkb.config import settings
from pkb.ingest import ingest_files


def test_frontmatter_only_document_removes_old_chunks_and_graph(monkeypatch, tmp_path):
    from pkb.graph import store as gstore
    from pkb.graph.schema import get_connection, init_schema

    note = tmp_path / "empty.md"
    note.write_text("---\ntitle: Empty\n---\n\n", encoding="utf-8")
    doc_id = "data/empty.md"
    db = tmp_path / "graph.sqlite"
    init_schema(str(db))
    monkeypatch.setattr(settings, "graph_db_path", str(db))
    with get_connection(str(db)) as conn:
        concept = gstore.upsert_concept(conn, name="Old")
        other = gstore.upsert_concept(conn, name="Other")
        gstore.upsert_document(conn, doc_id, "Old", "test")
        gstore.add_mention(conn, concept, doc_id, 0)
        conn.execute(
            "INSERT INTO concept_edges (src_id, dst_id, relation) VALUES (?, ?, 'related_to')",
            (concept, other),
        )
        conn.execute(
            "INSERT INTO concept_edge_evidence (doc_id, chunk_index, src_id, dst_id, relation) "
            "VALUES (?, 0, ?, ?, 'related_to')",
            (doc_id, concept, other),
        )
        conn.execute(
            "INSERT INTO extracted_chunks (doc_id, chunk_index, content_hash) VALUES (?, 0, 'old')",
            (doc_id,),
        )

    es = MagicMock()
    es.delete_by_query.return_value = {"deleted": 2}
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    stats = ingest_files([note], tmp_path, doc_id_prefix="data/")

    assert stats["files"] == 1
    assert stats["deleted"] == 2
    assert es.delete_by_query.call_args.kwargs["query"] == {"term": {"doc_id": doc_id}}
    with get_connection(str(db)) as conn:
        for table in ("documents", "concept_mentions", "concept_edge_evidence", "extracted_chunks"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE doc_id = ?", (doc_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("name,text", [("empty.txt", "  \n"), ("empty.md", "")])
def test_empty_text_document_deletes_without_graph(monkeypatch, tmp_path, name, text):
    note = tmp_path / name
    note.write_text(text, encoding="utf-8")
    monkeypatch.setattr(settings, "graph_db_path", str(tmp_path / "absent.sqlite"))
    es = MagicMock()
    es.delete_by_query.return_value = {"deleted": 1}
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    assert ingest_files([note], tmp_path)["deleted"] == 1


def test_excluded_and_unsupported_files_do_not_delete(monkeypatch, tmp_path):
    excluded = tmp_path / "_review" / "empty.md"
    excluded.parent.mkdir()
    excluded.write_text("", encoding="utf-8")
    unsupported = tmp_path / "empty.bin"
    unsupported.write_text("", encoding="utf-8")
    es = MagicMock()
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    assert ingest_files([excluded, unsupported], tmp_path)["files"] == 0
    es.delete_by_query.assert_not_called()


def test_failed_conversion_preserves_old_document(monkeypatch, tmp_path):
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"source")
    es = MagicMock()
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    def fail_read(_):
        raise OSError("conversion failed")

    monkeypatch.setattr("pkb.ingest.read_file_as_text", fail_read)
    with pytest.raises(OSError, match="conversion failed"):
        ingest_files([pdf], tmp_path)
    es.delete_by_query.assert_not_called()


@pytest.mark.parametrize("extension", ["pdf", "docx", "pptx", "xlsx", "html", "htm"])
def test_blank_conversion_raises_without_deleting(monkeypatch, tmp_path, extension):
    source = tmp_path / f"image_only.{extension}"
    source.write_bytes(b"source")
    es = MagicMock()
    monkeypatch.setattr("pkb.store.get_client", lambda: es)
    monkeypatch.setattr("pkb.ingest.read_file_as_text", lambda _: "")
    with pytest.raises(ValueError, match="텍스트 추출 결과가 비어 있습니다"):
        ingest_files([source], tmp_path)
    es.delete_by_query.assert_not_called()
