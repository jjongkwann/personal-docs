"""conversion_frontmatter — 변환 provenance 블록 생성·라운드트립 테스트."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pkb.ingest import conversion_frontmatter, parse_frontmatter


def test_conversion_frontmatter_roundtrips_and_preserves_body():
    body = "# 원본 제목\n\n본문 텍스트."
    block = conversion_frontmatter(Path("/어딘가/발표 자료.pdf"))

    meta, parsed_body = parse_frontmatter(block + body)

    assert meta["source"] == "발표 자료.pdf"
    assert meta["converted_from"] == ".pdf"
    assert meta["converted_at"] == date.today().isoformat()
    # title 미포함 — 본문 H1 추출(_extract_title)을 덮어쓰는 퇴행 방지
    assert "title" not in meta
    assert parsed_body.lstrip("\n") == body
