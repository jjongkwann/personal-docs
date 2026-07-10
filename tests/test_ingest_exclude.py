"""find_ingestable_files 제외 규칙: _review/_trash/_materials 디렉터리, dot-폴더."""

from __future__ import annotations

from pkb.ingest import find_ingestable_files


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
