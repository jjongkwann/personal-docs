"""pkb.ingest의 경로 기반 section_path/title 파생 테스트."""

from __future__ import annotations

from pathlib import Path

from pkb.ingest import _clean_path_name, _extract_title, derive_section_path_from_path

# ---------- _clean_path_name ----------

def test_clean_removes_numeric_prefix():
    assert _clean_path_name("4.2.6_청크_크기_선택") == "청크 크기 선택"


def test_clean_removes_simple_numeric_prefix():
    assert _clean_path_name("1_Introduction") == "Introduction"


def test_clean_keeps_name_without_numeric_prefix():
    assert _clean_path_name("신뢰·운영·측정") == "신뢰·운영·측정"


def test_clean_replaces_underscores_and_hyphens():
    assert _clean_path_name("some_nested-topic") == "some nested topic"


def test_clean_collapses_whitespace():
    assert _clean_path_name("  multi   space  ") == "multi space"


# ---------- derive_section_path_from_path ----------

def test_derive_rag_v2_deep_nested():
    base = Path("/tmp/project")
    fp = base / "data" / "study" / "rag-v2" / "4_신뢰·운영·측정" / "4.2_코퍼스·청킹·인덱스" / "4.2.6_청크_크기_선택.md"
    assert derive_section_path_from_path(fp, base) == "신뢰·운영·측정 > 코퍼스·청킹·인덱스 > 청크 크기 선택"


def test_derive_shallow_keeps_all():
    # cleaned = ['about', 'JK'] — len 2 이하이므로 전부 유지
    base = Path("/tmp/project")
    fp = base / "data" / "about" / "JK.md"
    assert derive_section_path_from_path(fp, base) == "about > JK"


def test_derive_outside_base_returns_empty():
    assert derive_section_path_from_path(Path("/other/foo.md"), Path("/tmp/project")) == ""


def test_derive_strips_extension():
    base = Path("/tmp/project")
    fp = base / "data" / "writing" / "drafts_01_wanted_career.md"
    # ['writing', 'drafts 01 wanted career'] → len 2, 전부 유지
    assert derive_section_path_from_path(fp, base) == "writing > drafts 01 wanted career"


def test_derive_handles_non_data_root():
    # data/ 하위가 아니어도 상대경로만 잘 계산되면 동작
    base = Path("/tmp/project")
    fp = base / "a" / "b" / "c" / "d.md"
    # parts = [a, b, c, d]; data/ 시작 아니므로 그대로.
    # cleaned len=4 → [2:] 이후 유지: [c, d]
    assert derive_section_path_from_path(fp, base) == "c > d"


# ---------- _extract_title ----------

def test_extract_title_from_h1():
    assert _extract_title("# Hello World\n\nbody", Path("/tmp/foo.md")) == "Hello World"


def test_extract_title_ignores_h2():
    assert _extract_title("## subheading\n\nbody", Path("/tmp/4.2.6_청크_크기_선택.md")) == "청크 크기 선택"


def test_extract_title_fallback_cleans_filename():
    assert _extract_title("no heading body", Path("/tmp/4.2.6_청크_크기_선택.md")) == "청크 크기 선택"


def test_extract_title_preserves_original_if_no_clean_result():
    # 숫자만인 파일명 → 정리 후 빈 문자열 → 원본 stem fallback
    assert _extract_title("no heading", Path("/tmp/123.md")) == "123"
