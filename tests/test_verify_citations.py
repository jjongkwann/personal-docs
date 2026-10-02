"""scripts/verify_citations.py 순수 로직 테스트 (네트워크는 주입으로 대체)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_citations", Path(__file__).resolve().parents[1] / "scripts" / "verify_citations.py"
)
vc = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_citations"] = vc  # dataclass가 모듈 네임스페이스를 찾음
_SPEC.loader.exec_module(vc)

NOTE = """# 노트
- Mitzenmacher 2018, NeurIPS 2018 (arXiv:1803.01474)
- EAGLE-2 (arXiv:2406.16858v2), 버전 3.14159 무시, 2026.08 날짜 무시
- CVE-2025-61510 와 CVE-2025-6514
"""


def test_extract_ids_filters_noise():
    assert vc.extract_arxiv_ids(NOTE) == ["1803.01474", "2406.16858"]
    assert vc.extract_cve_ids(NOTE) == ["CVE-2025-61510", "CVE-2025-6514"]


def test_verify_text_flags_missing_and_unbacked_venue():
    metas = {
        "1803.01474": vc.ArxivMeta(True, "Optimizing Learned Bloom Filters by Sandwiching", "2018-03", "Short note"),
        "2406.16858": vc.ArxivMeta(False),
    }
    cves = {"CVE-2025-61510": (False, "MITRE 404"), "CVE-2025-6514": (True, "MITRE state=PUBLISHED")}
    findings = vc.verify_text(NOTE, arxiv_fetch=lambda ids: metas, cve_fetch=lambda c: cves[c])
    by_ref = {f.ref: f for f in findings}
    assert by_ref["arXiv:1803.01474"].venue_claims == ["NeurIPS 2018"]
    assert by_ref["arXiv:1803.01474"].venue_verified is False
    assert by_ref["arXiv:2406.16858"].exists is False
    assert by_ref["CVE-2025-61510"].exists is False and by_ref["CVE-2025-6514"].exists is True


def test_upsert_section_is_idempotent():
    section = vc.render_section([vc.Finding("CVE-1", False, "x")], "2026-08-18")
    once = vc.upsert_section(NOTE, section)
    twice = vc.upsert_section(once, section)
    assert once == twice
    assert once.count(vc.SECTION_HEADER) == 1 and once.startswith(NOTE.rstrip("\n"))


LINK_NOTE = """# 노트
- [우아한형제들: 우리 팀은 카프카를 어떻게 사용하고 있을까](https://techblog.woowahan.com/17386/)
- [죽은 링크](https://techblog.woowahan.com/18854/)
- [엉뚱한 제목을 단 링크](https://example.com/x)
- [arXiv는 ID 검사로 이미 커버](https://arxiv.org/abs/1803.01474)
"""

PAGES = {
    "https://techblog.woowahan.com/17386/": (
        200, "우리 팀은 카프카를 어떻게 사용하고 있을까 | 우아한형제들 기술블로그",
    ),
    "https://techblog.woowahan.com/18854/": (404, ""),
    "https://example.com/x": (200, "카프카 컨슈머에 동적 쓰로틀링 적용하기"),
}


def test_extract_links_skips_arxiv_and_dedupes():
    assert [u for _, u in vc.extract_links(LINK_NOTE)] == [
        "https://techblog.woowahan.com/17386/",
        "https://techblog.woowahan.com/18854/",
        "https://example.com/x",
    ]


def test_check_link_catches_404_and_title_mismatch():
    fetch = PAGES.__getitem__
    good, dead, wrong = (
        vc.check_link(label, url, fetch=fetch) for label, url in vc.extract_links(LINK_NOTE)
    )
    assert good.exists is True
    assert dead.exists is False and "존재하지 않는" in dead.detail
    assert wrong.exists is False and "불일치" in wrong.detail


def test_unreachable_link_is_unverified_not_missing():
    f = vc.check_link("x", "https://x.test/", fetch=lambda u: (0, ""))
    assert f.exists is None and "⚠️" in vc.render_section([f], "2026-08-25")


@pytest.mark.parametrize("status,exists", [
    (200, True), (404, False), (410, False),
    (0, None), (403, None), (429, None), (500, None),
])
def test_d2_preserves_api_status_and_only_marks_missing_pages_absent(monkeypatch, status, exists):
    calls = []

    def get(url):
        calls.append(url)
        return status, b'{"postTitle": "Article title"}'

    monkeypatch.setattr(vc, "_get", get)
    finding = vc.check_link("Article title", "https://d2.naver.com/helloworld/12345")

    assert calls == ["https://d2.naver.com/api/v1/contents/12345"]
    assert finding.exists is exists
    if status == 200:
        assert "Article title" in finding.detail
    elif status:
        assert f"HTTP {status}" in finding.detail


def test_arxiv_outage_does_not_block_cve_and_link_checks():
    def boom(ids):
        raise vc.ArxivUnavailableError("arXiv API HTTP 0")

    findings = vc.verify_text(
        LINK_NOTE, arxiv_fetch=boom, cve_fetch=lambda c: (True, ""), link_fetch=PAGES.__getitem__
    )
    assert findings[0].ref == "arXiv API" and findings[0].exists is None
    assert sum(1 for f in findings if f.exists is False) == 2
