"""write_file frontmatter 검증 — 경고-only (저장 거부 분기 없음)."""

from __future__ import annotations

import pytest

from pkb.mcp_server import _frontmatter_warnings
from pkb.mcp_server import write_file as _write_file

# MCPServer @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
write_file = getattr(_write_file, "fn", _write_file)


def test_valid_frontmatter_no_warnings():
    content = "---\ntitle: 좋은 노트\ntags: [rag, es]\nexpires_at: 2026-12-31\n---\n\n본문\n"
    assert _frontmatter_warnings(content) == []


def test_missing_frontmatter_warns():
    warnings = _frontmatter_warnings("# 제목\n\n본문\n")
    assert len(warnings) == 1
    assert "frontmatter 없음" in warnings[0]


def test_bad_tags_warns():
    content = "---\ntitle: t\ntags: {a: 1}\n---\n본문\n"
    assert any("tags" in w for w in _frontmatter_warnings(content))
    # list 안에 비문자열이 섞여도 경고
    content = "---\ntitle: t\ntags: [rag, 3]\n---\n본문\n"
    assert any("tags" in w for w in _frontmatter_warnings(content))


def test_bad_expires_at_warns():
    content = "---\ntitle: t\nexpires_at: 언젠가\n---\n본문\n"
    assert any("expires_at" in w for w in _frontmatter_warnings(content))


@pytest.fixture
def in_tmp_data_root(monkeypatch, tmp_path):
    """CWD를 tmp_path로 바꾸고 tmp_path/data를 코퍼스 루트로 강제 (.env의 DATA_ROOT 무시)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pkb.config.settings.data_root", "data")
    (tmp_path / "data").mkdir()
    return tmp_path


def test_write_file_saves_despite_warnings(in_tmp_data_root):
    result = write_file("data/writing/x.md", "frontmatter 없는 본문", ingest=False)
    assert "저장 완료" in result
    assert "warning: frontmatter 없음" in result
    assert (in_tmp_data_root / "data" / "writing" / "x.md").exists()


def test_write_file_requires_dry_run_hash_before_edit(in_tmp_data_root):
    path = in_tmp_data_root / "data" / "writing" / "x.md"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")

    blocked = write_file("data/writing/x.md", "new\n", ingest=False)
    assert "expected_hash가 필요" in blocked
    assert path.read_text(encoding="utf-8") == "old\n"

    preview = write_file(
        "data/writing/x.md", "new\n", ingest=False, dry_run=True
    )
    assert "쓰기 미리보기" in preview
    previous_hash = preview.split("previous_hash: ", 1)[1].splitlines()[0]
    applied = write_file(
        "data/writing/x.md",
        "new\n",
        ingest=False,
        expected_hash=previous_hash,
    )
    assert "파일 저장 완료" in applied
    assert path.read_text(encoding="utf-8") == "new\n"
