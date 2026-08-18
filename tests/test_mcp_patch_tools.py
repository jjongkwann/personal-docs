"""read_file / patch_file 부분 편집 도구 + sync_corpus prune 기본 보류 회귀 테스트."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pkb.mcp_server import patch_file as _patch_file
from pkb.mcp_server import read_file as _read_file
from pkb.mcp_server import sync_corpus as _sync_corpus
from pkb.operations import SyncResult

read_file = getattr(_read_file, "fn", _read_file)
patch_file = getattr(_patch_file, "fn", _patch_file)
sync_corpus = getattr(_sync_corpus, "fn", _sync_corpus)

DOC = "data/writing/note.md"


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    (tmp_path / "data" / "writing").mkdir(parents=True)
    (tmp_path / "data" / "writing" / "note.md").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return tmp_path


def _hash_from(result: str) -> str:
    match = re.search(r"content_hash: ([0-9a-f]{64})", result)
    assert match, result
    return match.group(1)


def test_read_file_returns_hash_and_content(data_root):
    out = read_file(DOC)
    assert "alpha\nbeta\ngamma\n" in out
    assert _hash_from(out)


def test_read_file_rejects_outside_data(data_root):
    assert "오류" in read_file("../etc/passwd.md")
    assert "오류" in read_file("data/writing/missing.md")


def test_patch_file_replaces_exactly_once(data_root):
    h = _hash_from(read_file(DOC))
    out = patch_file(DOC, "beta", "BETA", expected_hash=h, ingest=False)
    assert "저장 완료" in out
    assert (data_root / "data/writing/note.md").read_text() == "alpha\nBETA\ngamma\n"


def test_patch_file_rejects_ambiguous_or_missing_old(data_root):
    h = _hash_from(read_file(DOC))
    assert "0회" in patch_file(DOC, "zeta", "x", expected_hash=h)
    assert "3회" in patch_file(DOC, "a\n", "x", expected_hash=h)  # alpha/beta/gamma 모두 "a\n"으로 끝남
    assert (data_root / "data/writing/note.md").read_text() == "alpha\nbeta\ngamma\n"


def test_patch_file_requires_expected_hash_and_locks(data_root):
    assert "expected_hash" in patch_file(DOC, "beta", "x")
    assert "optimistic lock" in patch_file(DOC, "beta", "x", expected_hash="0" * 64)


def test_patch_file_dry_run_shows_diff_without_writing(data_root):
    out = patch_file(DOC, "beta", "BETA", dry_run=True)
    assert "미리보기" in out and "-beta" in out and "+BETA" in out
    assert (data_root / "data/writing/note.md").read_text() == "alpha\nbeta\ngamma\n"


def test_patch_file_rejects_new_file(data_root):
    assert "write_file" in patch_file("data/writing/new.md", "a", "b")


# ---------- sync_corpus: prune만 confirm 게이트 ----------


@pytest.fixture
def fake_sync(monkeypatch, tmp_path):
    stats = {k: 0 for k in ("files", "reused", "moved", "embedded", "added", "metadata_updated", "deleted")}
    stale = ("data/gone/a.md", "data/gone/b.md")
    monkeypatch.setattr("pkb.store.get_client", lambda: MagicMock())
    monkeypatch.setattr("pkb.config.data_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(
        "pkb.operations.sync_tree", lambda es, root, prefix: SyncResult(Path(tmp_path), stats, stale)
    )
    pruned: list = []
    monkeypatch.setattr("pkb.operations.prune_documents", lambda es, ids: pruned.extend(ids))
    monkeypatch.setattr("pkb.mcp_server._graph_prune_summary", lambda es: "")
    return pruned


def test_sync_corpus_never_prunes_without_confirm(fake_sync):
    out = sync_corpus()
    assert "삭제 보류" in out and "data/gone/a.md" in out
    assert fake_sync == []


def test_sync_corpus_prunes_only_with_confirm(fake_sync):
    out = sync_corpus(confirm_prune=True)
    assert "2개 삭제" in out
    assert fake_sync == ["data/gone/a.md", "data/gone/b.md"]
