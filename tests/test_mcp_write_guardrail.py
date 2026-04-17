"""MCP write_file 가드레일: data/ 하위 강제 + .md 확장자 강제."""

from __future__ import annotations

import pytest

# FastMCP @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근,
# 아니면 그대로 호출.
from pkb.mcp_server import write_file as _write_file


def _call(*args, **kwargs):
    fn = getattr(_write_file, "fn", _write_file)
    return fn(*args, **kwargs)


@pytest.fixture
def in_tmp_data_root(monkeypatch, tmp_path):
    """CWD를 tmp_path로 바꾸고 tmp_path/data 폴더를 준비."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def test_write_file_rejects_outside_data(in_tmp_data_root):
    result = _call("outside.md", "content", ingest=False)
    assert "오류" in result
    assert "data/ 하위" in result


def test_write_file_rejects_path_traversal(in_tmp_data_root):
    result = _call("data/../escape.md", "content", ingest=False)
    assert "오류" in result


def test_write_file_rejects_non_md_extension(in_tmp_data_root):
    result = _call("data/note.txt", "content", ingest=False)
    assert "오류" in result
    assert "마크다운" in result or ".md" in result


def test_write_file_creates_md_within_data(in_tmp_data_root):
    result = _call("data/writing/foo.md", "hello world", ingest=False)
    assert "저장 완료" in result
    assert (in_tmp_data_root / "data" / "writing" / "foo.md").read_text() == "hello world"


def test_write_file_creates_parents(in_tmp_data_root):
    # 새 카테고리 폴더가 없어도 자동 생성되어야 함
    result = _call("data/new_cat/sub/nested.md", "x", ingest=False)
    assert "저장 완료" in result
    assert (in_tmp_data_root / "data" / "new_cat" / "sub" / "nested.md").exists()
