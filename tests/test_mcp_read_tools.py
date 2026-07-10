"""mcp_server 읽기 도구(get_document/list_documents) 토큰 다이어트 회귀 테스트.

ES 의존 로직은 MagicMock으로, chunk_range 파서는 순수 함수 단위 테스트로 검증.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pkb.mcp_server import _parse_chunk_range, _render_document
from pkb.mcp_server import get_document as _get_document

# FastMCP @mcp.tool() 데코레이터 호환: 함수가 래핑돼 있으면 .fn 속성으로 접근.
get_document = getattr(_get_document, "fn", _get_document)

# ---------- _parse_chunk_range ----------


def test_parse_chunk_range_single():
    assert _parse_chunk_range("3") == (3, 3)


def test_parse_chunk_range_range():
    assert _parse_chunk_range("3-7") == (3, 7)


def test_parse_chunk_range_invalid_non_numeric():
    assert _parse_chunk_range("abc") is None


def test_parse_chunk_range_invalid_reversed():
    assert _parse_chunk_range("7-3") is None


def test_parse_chunk_range_invalid_negative():
    assert _parse_chunk_range("-1") is None


# ---------- _render_document ----------

_SOURCES = [
    {
        "title": "테스트 문서",
        "category": "study",
        "date_modified": "2026-01-01T00:00:00",
        "chunk_index": 0,
        "section_path": "서론",
        "content": "이것은 첫 번째 청크의 본문입니다. " * 20,
    },
    {
        "title": "테스트 문서",
        "category": "study",
        "date_modified": "2026-01-01T00:00:00",
        "chunk_index": 1,
        "section_path": "본론",
        "content": "이것은 두 번째 청크의 본문입니다. " * 20,
    },
]


def test_render_document_default_is_toc_without_content():
    text = _render_document("data/study/x.md", _SOURCES, include_content=False, chunk_range="")
    assert "테스트 문서" in text
    assert "#0 서론" in text
    assert "#1 본론" in text
    # 목차 모드는 본문 전문을 포함하지 않는다
    assert _SOURCES[0]["content"] not in text
    assert _SOURCES[1]["content"] not in text


def test_render_document_include_content_true_has_full_text():
    text = _render_document("data/study/x.md", _SOURCES, include_content=True, chunk_range="")
    assert _SOURCES[0]["content"] in text
    assert _SOURCES[1]["content"] in text


def test_render_document_chunk_range_returns_only_selected():
    text = _render_document("data/study/x.md", _SOURCES, include_content=False, chunk_range="1")
    assert _SOURCES[1]["content"] in text
    assert _SOURCES[0]["content"] not in text


def test_render_document_chunk_range_invalid_returns_error():
    text = _render_document("data/study/x.md", _SOURCES, include_content=False, chunk_range="bad")
    assert "오류" in text


# ---------- get_document (MagicMock ES) ----------


def _mock_es_with_hits(sources: list[dict]):
    es = MagicMock()
    es.search.return_value = {
        "hits": {"hits": [{"_source": s} for s in sources]}
    }
    return es


def test_get_document_default_toc_mode_excludes_content(monkeypatch):
    es = _mock_es_with_hits(_SOURCES)
    monkeypatch.setattr("pkb.store.get_client", lambda: es)

    result = get_document("data/study/x.md")

    assert _SOURCES[0]["content"] not in result
    assert _SOURCES[1]["content"] not in result
    assert "#0 서론" in result


def test_get_document_not_found(monkeypatch):
    es = _mock_es_with_hits([])
    monkeypatch.setattr("pkb.store.get_client", lambda: es)

    result = get_document("data/study/missing.md")
    assert "찾을 수 없습니다" in result
