"""scripts/verify_citations.py 순수 로직 테스트 (네트워크는 주입으로 대체)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
