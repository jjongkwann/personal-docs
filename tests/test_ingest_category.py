"""동적 카테고리: 코퍼스 최상위 폴더명 = 카테고리, 화이트리스트 없음."""

from pathlib import Path

from pkb.ingest import _extract_category

BASE = Path("/vault/PKB")


def test_core_folder():
    assert _extract_category(BASE / "study/rag/a.md", BASE) == "study"


def test_new_folder_becomes_category():
    assert _extract_category(BASE / "journal/2026-07-08.md", BASE) == "journal"


def test_nested_file_uses_top_folder():
    assert _extract_category(BASE / "journal/trading/x.md", BASE) == "journal"


def test_root_file_is_misc():
    assert _extract_category(BASE / "loose.md", BASE) == "misc"


def test_hidden_dir_is_misc():
    assert _extract_category(BASE / ".eval/snapshot.md", BASE) == "misc"


def test_outside_base_is_misc():
    assert _extract_category(Path("/elsewhere/x.md"), BASE) == "misc"
