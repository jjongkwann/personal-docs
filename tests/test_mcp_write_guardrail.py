"""MCP 쓰기 도구 가드레일: data/ 하위 강제 + .md 확장자 + 숨김 폴더 차단."""

from __future__ import annotations

import pytest

# MCPServer @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근,
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


def test_write_file_rejects_new_file_in_legacy_inbox(in_tmp_data_root):
    # strict_policy 기본값(True)에서 daily-research/ 신규 저장은 교육적 에러로 거부
    result = _call("data/daily-research/2026-08-08-note.md", "content", ingest=False)
    assert "오류" in result
    assert "레거시 인박스" in result
    assert "research/" in result  # 에러가 올바른 경로를 안내해야 함
    assert not (in_tmp_data_root / "data" / "daily-research" / "2026-08-08-note.md").exists()


def test_write_file_dry_run_also_rejects_legacy_inbox(in_tmp_data_root):
    # 미리보기 단계에서 먼저 배우도록 dry_run에서도 동일하게 거부
    result = _call("data/daily-research/x.md", "content", dry_run=True)
    assert "오류" in result
    assert "레거시 인박스" in result


def test_write_file_allows_editing_existing_legacy_inbox_file(in_tmp_data_root):
    # 기존 파일 편집(이관·아카이브 등)은 허용
    target = in_tmp_data_root / "data" / "daily-research" / "old.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    preview = _call("data/daily-research/old.md", "updated", dry_run=True)
    assert "오류" not in preview
    assert "쓰기 미리보기" in preview


def test_write_file_legacy_inbox_allowed_when_strict_off(in_tmp_data_root):
    # 호환 모드(strict_policy=False)는 기존 동작 유지
    result = _call("data/daily-research/compat.md", "x", strict_policy=False)
    assert "저장 완료" in result


def test_write_file_ingest_appends_graph_nudge(in_tmp_data_root, monkeypatch):
    monkeypatch.setattr(
        "pkb.ingest.ingest_files",
        lambda files, base_dir, **kwargs: {
            "files": 1, "reused": 0, "moved": 0, "embedded": 0,
            "added": 1, "metadata_updated": 0, "deleted": 0,
        },
    )
    result = _call("data/writing/foo.md", "hello world", ingest=True)
    assert 'graph_list_chunks(doc_id="data/writing/foo.md", pending_only=True)' in result
    assert "graph_store_concepts" in result


def test_write_file_no_ingest_omits_graph_nudge(in_tmp_data_root):
    result = _call("data/writing/foo.md", "hello world", ingest=False)
    assert "그래프 미추출" not in result


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


def test_convert_empty_text_returns_self_transcription_guide(in_tmp_data_root, monkeypatch):
    # 스캔 PDF 등 텍스트 추출 결과가 빈 파일 — frontmatter-only 파일을 만들지 않고 셀프 전사 안내
    monkeypatch.setattr("pkb.ingest.read_file_as_text", lambda _p: "")
    src = in_tmp_data_root / "scan.pdf"
    src.write_bytes(b"%PDF-1.4")
    result = _call_convert(str(src), category="study", ingest=False)
    assert "Read 도구" in result
    assert "write_file" in result
    assert "source: scan.pdf" in result
    # 파일은 생성되지 않음 — 안내만 반환
    assert not (in_tmp_data_root / "data" / "study" / "scan.md").exists()


def test_convert_image_returns_self_transcription_guide(in_tmp_data_root):
    # 이미지는 에러 대신 셀프 전사 안내 (Read로 보고 write_file로 작성)
    src = in_tmp_data_root / "diagram.png"
    src.write_bytes(b"\x89PNG")
    result = _call_convert(str(src), category="study", ingest=False)
    assert "Read 도구" in result
    assert "write_file" in result
    # 변환물과 동일한 provenance frontmatter 템플릿 안내
    assert "source: diagram.png" in result
    assert "converted_from: .png" in result
    # 파일은 생성되지 않음 — 안내만 반환
    assert not (in_tmp_data_root / "data" / "study" / "diagram.md").exists()
