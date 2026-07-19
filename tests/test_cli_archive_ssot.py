"""CLI archive/restore도 MCP와 동일하게 원본 frontmatter를 SSOT로 사용한다."""

from typer.testing import CliRunner

from pkb.cli import app

runner = CliRunner()

_STATS = {
    "files": 1,
    "reused": 0,
    "moved": 0,
    "embedded": 0,
    "added": 0,
    "metadata_updated": 1,
    "deleted": 0,
}


def test_cli_archive_and_restore_roundtrip_frontmatter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    note = tmp_path / "data" / "study" / "note.md"
    note.parent.mkdir(parents=True)
    original = "---\ntitle: 노트\n---\n\n본문\n"
    note.write_text(original, encoding="utf-8")

    monkeypatch.setattr("pkb.ingest.ingest_files", lambda *a, **k: dict(_STATS))
    monkeypatch.setattr("pkb.search_log.log_change", lambda *a, **k: None)
    monkeypatch.setattr("pkb.store.get_client", lambda: object())
    restored: list[str] = []
    monkeypatch.setattr(
        "pkb.store.restore_document", lambda es, doc_id: restored.append(doc_id) or 1
    )

    archived = runner.invoke(
        app, ["archive", "data/study/note.md", "--reason", "옛 문서"]
    )
    assert archived.exit_code == 0, archived.output
    assert "frontmatter 기록" in archived.output
    assert "archived_at:" in note.read_text(encoding="utf-8")

    restored_result = runner.invoke(app, ["restore", "data/study/note.md"])
    assert restored_result.exit_code == 0, restored_result.output
    assert "frontmatter 제거" in restored_result.output
    assert note.read_text(encoding="utf-8") == original
    assert restored == ["data/study/note.md"]


def test_reindex_target_rejects_obsidian_path_escape(tmp_path, monkeypatch):
    from pkb.documents import DocumentPathError, resolve_reindex_target

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("pkb.config.settings.obsidian_path", str(vault))

    try:
        resolve_reindex_target("obsidian/../outside.md")
    except DocumentPathError:
        pass
    else:
        raise AssertionError("obsidian 경로 이탈을 허용했습니다")
