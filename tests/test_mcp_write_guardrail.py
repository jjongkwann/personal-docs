"""MCP 쓰기 도구 가드레일: data/ 하위 강제 + .md 확장자 + 숨김 폴더 차단."""

from __future__ import annotations

import pytest

# FastMCP @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근,
# 아니면 그대로 호출.
from pkb.mcp_server import convert_and_ingest as _convert_and_ingest
from pkb.mcp_server import write_file as _write_file


def _call(*args, **kwargs):
    fn = getattr(_write_file, "fn", _write_file)
    return fn(*args, **kwargs)


def _call_convert(*args, **kwargs):
    fn = getattr(_convert_and_ingest, "fn", _convert_and_ingest)
    return fn(*args, **kwargs)


@pytest.fixture
def in_tmp_data_root(monkeypatch, tmp_path):
    """CWD를 tmp_path로 바꾸고 tmp_path/data를 코퍼스 루트로 강제 (.env의 DATA_ROOT 무시)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
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


def test_convert_rejects_hidden_category(in_tmp_data_root):
    src = in_tmp_data_root / "src.md"
    src.write_text("hello")
    result = _call_convert(str(src), category=".graph", ingest=False)
    assert "오류" in result


def test_convert_rejects_escape_via_category(in_tmp_data_root):
    src = in_tmp_data_root / "src.md"
    src.write_text("hello")
    result = _call_convert(str(src), category="../escape", ingest=False)
    assert "오류" in result


def test_convert_rejects_absolute_category(in_tmp_data_root):
    # 절대경로 category는 data_root와 join돼도 절대경로로 남아
    # resolve + is_relative_to 검증에서 거부된다.
    src = in_tmp_data_root / "src.md"
    src.write_text("hello")
    result = _call_convert(str(src), category="/etc", ingest=False)
    assert "오류" in result


def test_convert_rejects_nested_traversal(in_tmp_data_root):
    src = in_tmp_data_root / "src.md"
    src.write_text("hello")
    result = _call_convert(str(src), category="study/../../escape", ingest=False)
    assert "오류" in result


def test_convert_accepts_new_category_with_subfolder(in_tmp_data_root):
    src = in_tmp_data_root / "src.md"
    src.write_text("hello")
    result = _call_convert(str(src), category="study/payments", ingest=False)
    assert "변환 완료" in result
    assert (in_tmp_data_root / "data" / "study" / "payments" / "src.md").exists()
