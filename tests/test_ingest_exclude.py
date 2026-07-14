"""find_ingestable_files 제외 규칙: _review/_trash/_materials/_archive/_origin 디렉터리, dot-폴더."""

from __future__ import annotations

import pytest

from pkb.ingest import find_ingestable_files, process_file


def test_materials_dir_excluded(tmp_path):
    # 강의 PDF 원본(_materials)은 같은 폴더 _extracted md가 전량 존재 → 이중 색인 방지
    materials = tmp_path / "study" / "_materials"
    materials.mkdir(parents=True)
    (materials / "lecture.pdf").write_text("dummy")
    extracted = tmp_path / "study" / "lecture_extracted.md"
    extracted.write_text("# Lecture\nbody")

    files = find_ingestable_files(tmp_path)
    assert extracted in files
    assert all("_materials" not in f.parts for f in files)


def test_dot_dir_excluded_in_directory_walk(tmp_path):
    hidden = tmp_path / ".foo"
    hidden.mkdir()
    (hidden / "theme.md").write_text("hidden")
    visible = tmp_path / "note.md"
    visible.write_text("visible")

    files = find_ingestable_files(tmp_path)
    assert visible in files
    assert not any(".foo" in f.parts for f in files)


def test_dot_dir_excluded_for_single_file_path(tmp_path):
    hidden_dir = tmp_path / ".obsidian"
    hidden_dir.mkdir()
    f = hidden_dir / "theme.md"
    f.write_text("hidden")

    assert find_ingestable_files(f) == []


def test_archive_dir_excluded(tmp_path, monkeypatch):
    """_archive는 보관 문서 — 색인 제외 (2026-07-11 감사: 197청크 누수 발견)."""
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path))
    d = tmp_path / "topic" / "_archive"
    d.mkdir(parents=True)
    (d / "old.md").write_text("x", encoding="utf-8")
    (tmp_path / "topic" / "cur.md").write_text("y", encoding="utf-8")
    names = {f.name for f in find_ingestable_files(tmp_path)}
    assert names == {"cur.md"}


def test_archive_single_file_add_excluded(tmp_path):
    # 단일 파일 인자에도 제외 규칙 적용 — pkb add로 보관 문서가 재색인되는 플래핑 방지
    d = tmp_path / "topic" / "_archive"
    d.mkdir(parents=True)
    f = d / "old.md"
    f.write_text("x", encoding="utf-8")
    assert find_ingestable_files(f) == []


@pytest.mark.parametrize("reserved", ["_review", "_trash", "_archive", "_origin"])
def test_process_file_rejects_reserved_dirs(tmp_path, monkeypatch, reserved):
    """탐색을 우회하는 직접 인제스트(write_file/add_document)도 예약 디렉터리는 차단.

    이 관문이 없으면 data/_review/draft.md가 색인됐다가 다음 sync에서 stale로 조용히 삭제된다.
    """
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path))
    d = tmp_path / "topic" / reserved
    d.mkdir(parents=True)
    f = d / "draft.md"
    f.write_text("# Draft\n본문", encoding="utf-8")

    assert process_file(f, tmp_path) == []
    assert process_file(tmp_path / "topic" / reserved / "draft.md", tmp_path) == []


def test_process_file_accepts_normal_path(tmp_path, monkeypatch):
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path))
    f = tmp_path / "topic" / "note.md"
    f.parent.mkdir(parents=True)
    f.write_text("# Note\n본문", encoding="utf-8")

    assert process_file(f, tmp_path)  # 정상 경로는 그대로 청크 생성


def test_origin_dir_excluded(tmp_path, monkeypatch):
    """_origin은 외부 원본 보관소 — 소화 노트만 색인, 원본은 색인 제외."""
    monkeypatch.setattr("pkb.config.settings.data_root", str(tmp_path))
    d = tmp_path / "topic" / "_origin"
    d.mkdir(parents=True)
    (d / "raw.md").write_text("x", encoding="utf-8")
    (tmp_path / "topic" / "digested.md").write_text("y", encoding="utf-8")
    names = {f.name for f in find_ingestable_files(tmp_path)}
    assert names == {"digested.md"}
