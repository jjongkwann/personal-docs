"""PDF 페이지 보존 변환 테스트 — ES 없이 pdfminer 실추출로 검증.

픽스처는 바이너리 커밋 대신 raw bytes로 최소 유효 PDF를 tmp에 직접 조립한다
(pdfminer가 실제로 텍스트를 추출할 수 있는 수준: xref/trailer 오프셋 계산 포함).
"""

from __future__ import annotations

from pathlib import Path

from pkb.ingest import _join_pdf_pages, read_file_as_text


def _build_pdf(pages: list[str]) -> bytes:
    """페이지당 텍스트 한 줄짜리 최소 유효 PDF 바이트 조립 (Helvetica, ASCII 전용)."""
    objs: list[bytes] = []
    n = len(pages)
    font_num = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())  # 2
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {4 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (num, body)
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref_pos,
    )
    return bytes(out)


def test_read_pdf_emits_page_markers(tmp_path: Path):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(_build_pdf(["Hello page one", "Hello page two"]))

    text = read_file_as_text(pdf)

    assert "## p.1" in text
    assert "## p.2" in text
    assert "Hello page one" in text
    assert "Hello page two" in text
    # 마커가 헤딩 청커의 섹션 경계가 되도록 페이지 순서 유지
    assert text.index("## p.1") < text.index("Hello page one") < text.index("## p.2")


def test_join_pdf_pages_skips_empty_but_keeps_numbering():
    # 텍스트 없는 페이지(스캔 이미지 등)는 마커를 만들지 않되 페이지 번호는 소비
    joined = _join_pdf_pages(["first", "", "third"])
    assert "## p.1\n\nfirst" in joined
    assert "## p.2" not in joined
    assert "## p.3\n\nthird" in joined


def test_join_pdf_pages_all_empty_returns_empty():
    # 전체가 빈 PDF → 빈 문자열 → process_file이 빈 파일로 취급
    assert _join_pdf_pages(["", ""]) == ""
