"""macOS NFD 경로가 doc_id/category/title/section을 두 벌로 가르지 않는지."""

import unicodedata

from pkb.ingest import (
    _extract_category,
    _extract_title,
    derive_section_path_from_path,
    process_file,
)


def test_nfd_path_yields_nfc_identifiers(tmp_path):
    # macOS가 돌려주는 형태를 재현: NFD로 분해된 한글 폴더/파일명
    cat_nfd = unicodedata.normalize("NFD", "데이터베이스")
    name_nfd = unicodedata.normalize("NFD", "샤딩_전략")
    d = tmp_path / cat_nfd
    d.mkdir()
    f = d / f"{name_nfd}.md"
    f.write_text("본문뿐, 헤딩 없음.", encoding="utf-8")

    assert _extract_category(f, tmp_path) == "데이터베이스"  # NFC
    assert _extract_title("헤딩 없는 본문", f) == unicodedata.normalize("NFC", "샤딩 전략")
    section = derive_section_path_from_path(f, tmp_path)
    assert section == unicodedata.normalize("NFC", section)

    chunks = process_file(f, base_dir=tmp_path, doc_id_prefix="data/")
    assert chunks
    for c in chunks:
        for field in ("doc_id", "source_path", "category", "title", "section_path"):
            v = c[field]
            assert v == unicodedata.normalize("NFC", v), field


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        test_nfd_path_yields_nfc_identifiers(Path(td))
    print("ok")
    sys.exit(0)
