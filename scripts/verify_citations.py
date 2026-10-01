#!/usr/bin/env python3
"""Verify arXiv / CVE citations in Markdown notes against public registries.

Auto-generated research notes hallucinate: non-existent arXiv IDs, wrong titles for
real IDs, invented CVE numbers, and venue labels ("NeurIPS 2024") the preprint never
carried. This script extracts every ``arXiv:YYMM.NNNNN`` and ``CVE-YYYY-NNNN`` from
the given notes, resolves them (arXiv Atom API, MITRE CVE Services), and reports:

* whether the ID exists,
* the registered title and publication month (for the reader to compare),
* whether a venue claimed on the same line is backed by the arXiv
  ``comment``/``journal_ref`` metadata (otherwise "unverified" → treat as preprint).

Blog citations hallucinate the same way, but as URLs: a real article title pinned to a
guessed numeric ID. Every Markdown link is fetched — 404 means the page does not exist,
and the page ``<title>`` is compared against the link text so a live-but-different
article is flagged too. SPAs that answer 200 for any path (NAVER D2) are resolved
through their content API instead.

Usage::

    python scripts/verify_citations.py NOTE.md [NOTE2.md ...]          # report to stdout
    python scripts/verify_citations.py --write NOTE.md                  # also append/replace
                                                                        # "## 인용 검증 메모"

Exit status 1 if any citation is non-existent, 0 otherwise. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ARXIV_RE = re.compile(r"(?<![\d.])(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?(?![\d.])")
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b")
VENUE_RE = re.compile(
    r"\b(NeurIPS|NIPS|ICML|ICLR|ACL|EMNLP|NAACL|COLING|AAAI|IJCAI|KDD|WWW|SIGIR|CIKM|WSDM|"
    r"SIGMOD|VLDB|PVLDB|ICDE|EDBT|PODS|OSDI|SOSP|NSDI|EuroSys|ATC|FAST|USENIX|CCS|S&P|NDSS|"
    r"CVPR|ICCV|ECCV|MLSys|FOCS|STOC|SODA|PODC|OPODIS|DISC|SPAA|ISCA|MICRO|ASPLOS|HPCA|"
    r"COLM|TMLR|JMLR|CCNC|INFOCOM|SIGCOMM|IMC|CoNEXT)\b(?:['’]?\s?(20\d{2}|\d{2}))?",
    re.IGNORECASE,
)
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
TOKEN_RE = re.compile(r"[0-9a-z가-힣]{2,}")
D2_RE = re.compile(r"https?://d2\.naver\.com/helloworld/(\d+)")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
SECTION_HEADER = "## 인용 검증 메모"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
USER_AGENT = "pkb-verify-citations/1 (+https://github.com/jjongkwann/personal-docs)"


class ArxivUnavailable(RuntimeError):
    """arXiv API에 도달하지 못함 — 인용이 틀렸다는 뜻이 아니다."""


@dataclass
class ArxivMeta:
    exists: bool
    title: str = ""
    published: str = ""  # YYYY-MM
    comment: str = ""
    journal_ref: str = ""


@dataclass
class Finding:
    ref: str
    exists: bool | None  # None = 미확인 (네트워크·차단 등으로 판정 불가)
    detail: str
    venue_claims: list[str] = field(default_factory=list)
    venue_verified: bool | None = None  # None = no claim
    title_overlap: float | None = None


def _get(url: str, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc).encode()  # 0 = 도달 실패 (판정 불가)


def extract_arxiv_ids(text: str) -> list[str]:
    """Return unique arXiv IDs (version stripped) that look like real yymm.number."""
    out: list[str] = []
    for yy, mm, num in ARXIV_RE.findall(text):
        if not (1 <= int(mm) <= 12) or not (7 <= int(yy) <= 35):
            continue
        ref = f"{yy}{mm}.{num}"
        if ref not in out:
            out.append(ref)
    return out


def extract_cve_ids(text: str) -> list[str]:
    return list(dict.fromkeys(CVE_RE.findall(text)))


def fetch_arxiv(ids: list[str], sleep: float = 3.0) -> dict[str, ArxivMeta]:
    """Batch-resolve IDs. Missing entries mean the ID does not exist."""
    found: dict[str, ArxivMeta] = {}
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        if i:
            time.sleep(sleep)  # arXiv API etiquette
        url = (
            "https://export.arxiv.org/api/query?id_list="
            + ",".join(batch)
            + f"&max_results={len(batch)}"
        )
        status, body = _get(url)
        if status != 200:
            time.sleep(sleep)
            status, body = _get(url)
        if status != 200:
            raise ArxivUnavailable(f"arXiv API HTTP {status}")
        for entry in ET.fromstring(body).iter(f"{ATOM}entry"):
            raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
            if "/abs/" not in raw_id:
                continue  # API error entry for malformed IDs
            ref = re.sub(r"v\d+$", "", raw_id.rsplit("/abs/", 1)[1])
            found[ref] = ArxivMeta(
                exists=True,
                title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
                published=(entry.findtext(f"{ATOM}published") or "")[:7],
                comment=" ".join((entry.findtext(f"{ARXIV_NS}comment") or "").split()),
                journal_ref=" ".join((entry.findtext(f"{ARXIV_NS}journal_ref") or "").split()),
            )
    return {ref: found.get(ref, ArxivMeta(exists=False)) for ref in ids}


def fetch_cve(cve_id: str) -> tuple[bool, str]:
    """MITRE CVE Services: 404 = no record at all (reserved IDs still return 200)."""
    status, body = _get(f"https://cveawg.mitre.org/api/cve/{cve_id}")
    if status == 404:
        return False, "MITRE 404 (레코드 없음)"
    if status != 200:
        return True, f"MITRE HTTP {status} (미확인)"
    try:
        state = json.loads(body)["cveMetadata"]["state"]
    except (KeyError, ValueError):
        state = "?"
    return True, f"MITRE state={state}"


def extract_links(text: str) -> list[tuple[str, str]]:
    """Unique (link text, URL) pairs. arXiv is already covered by the ID check."""
    out: list[tuple[str, str]] = []
    for label, url in LINK_RE.findall(text):
        url = url.rstrip(".,")
        if "arxiv.org" in url or (label, url) in out:
            continue
        out.append((label, url))
    return out


def page_title(url: str) -> tuple[int, str]:
    """HTTP status and the page's own title. Returns status 0 when unreachable."""
    if m := D2_RE.match(url):
        # D2는 SPA라 없는 글도 200 + 빈 껍데기를 준다. 콘텐츠 API가 진짜 판정자다.
        status, body = _get(f"https://d2.naver.com/api/v1/contents/{m.group(1)}")
        if status != 200:
            return 404, ""
        try:
            return 200, json.loads(body).get("postTitle", "")
        except ValueError:
            return 200, ""
    status, body = _get(url)
    if status != 200:
        return status, ""
    m = TITLE_RE.search(body.decode("utf-8", "ignore"))
    return 200, " ".join(re.sub(r"<[^>]+>", " ", m.group(1)).split()) if m else ""


def label_matches_title(label: str, title: str) -> float:
    """Share of the page title's tokens that also appear in the note's link text."""
    words = set(TOKEN_RE.findall(title.lower()))
    if not words:
        return 1.0  # 제목 없음 → 대조 불가, 통과시키고 상태 코드만 믿는다
    ctx = set(TOKEN_RE.findall(label.lower()))
    return len(words & ctx) / len(words)


def check_link(label: str, url: str, *, fetch=page_title) -> Finding:
    status, title = fetch(url)
    if status == 0:
        return Finding(url, None, "도달 실패 (네트워크·차단)")
    if status == 404 or status == 410:
        return Finding(url, False, f"HTTP {status} — 존재하지 않는 URL")
    if status != 200:
        return Finding(url, None, f"HTTP {status} (미확인)")
    if not title:
        return Finding(url, True, "HTTP 200 · 제목 없음 — 본문 대조 필요")
    overlap = label_matches_title(label, title)
    detail = f"HTTP 200 · 실제 제목 “{title}”"
    if overlap < 0.4:
        return Finding(url, False, detail + " — 노트의 링크 텍스트와 불일치")
    return Finding(url, True, detail)


def context_lines(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


def venue_claims(lines: list[str]) -> list[str]:
    claims: list[str] = []
    for line in lines:
        for name, year in VENUE_RE.findall(line):
            claim = f"{name} {year}".strip()
            if claim not in claims:
                claims.append(claim)
    return claims


def venue_backed(claims: list[str], meta: ArxivMeta) -> bool:
    haystack = f"{meta.comment} {meta.journal_ref}".lower()
    return all(claim.split()[0].lower() in haystack for claim in claims)


def title_overlap(lines: list[str], title: str) -> float:
    """Share of the registered title's content words that appear near the citation."""
    words = {w for w in re.findall(r"[a-z]{4,}", title.lower())}
    if not words:
        return 1.0
    ctx = " ".join(lines).lower()
    return sum(1 for w in words if w in ctx) / len(words)


def verify_text(
    text: str, *, arxiv_fetch=fetch_arxiv, cve_fetch=fetch_cve, link_fetch=page_title
) -> list[Finding]:
    findings: list[Finding] = []
    arxiv_ids = extract_arxiv_ids(text)
    try:
        metas = arxiv_fetch(arxiv_ids) if arxiv_ids else {}
    except ArxivUnavailable as exc:
        # arXiv가 죽어도 CVE·링크 검증은 계속한다.
        findings.append(Finding("arXiv API", None, f"{exc} — arXiv 인용 미검증"))
        arxiv_ids, metas = [], {}
    for ref in arxiv_ids:
        meta = metas[ref]
        lines = context_lines(text, ref)
        if not meta.exists:
            findings.append(Finding(f"arXiv:{ref}", False, "비실존 ID (arXiv API에 없음)"))
            continue
        claims = venue_claims(lines)
        detail = f"“{meta.title}” ({meta.published})"
        if meta.journal_ref or meta.comment:
            detail += f" · 메타: {meta.journal_ref or meta.comment}"
        findings.append(
            Finding(
                f"arXiv:{ref}",
                True,
                detail,
                venue_claims=claims,
                venue_verified=venue_backed(claims, meta) if claims else None,
                title_overlap=title_overlap(lines, meta.title),
            )
        )
    for cve in extract_cve_ids(text):
        exists, detail = cve_fetch(cve)
        findings.append(Finding(cve, exists, detail))
    for label, url in extract_links(text):
        findings.append(check_link(label, url, fetch=link_fetch))
    return findings


def render_section(findings: list[Finding], today: str) -> str:
    lines = [
        SECTION_HEADER,
        "",
        f"자동 검증 {today} (`scripts/verify_citations.py`, arXiv API·MITRE CVE·링크 조회). "
        "venue 미확인 = arXiv 메타(comment/journal_ref)에 근거 없음 → 확인 전까지 'arXiv preprint'로 취급. "
        "제목·수치·저자 대조는 사람이 한다.",
        "",
    ]
    if not findings:
        lines.append("- 검증 대상 인용(arXiv/CVE ID·링크) 없음.")
    for f in findings:
        mark = {True: "✅", False: "❌", None: "⚠️"}[f.exists]
        line = f"- {mark} `{f.ref}` — {f.detail}"
        if f.venue_claims:
            verdict = "확인" if f.venue_verified else "미확인"
            line += f" · venue 주장 {', '.join(f.venue_claims)}: {verdict}"
        if f.title_overlap is not None and f.title_overlap < 0.4:
            line += " · ⚠️ 노트 문맥과 제목 어휘 겹침 낮음 — ID↔제목 대조 필요"
        lines.append(line)
    return "\n".join(lines) + "\n"


def upsert_section(text: str, section: str) -> str:
    idx = text.find(f"\n{SECTION_HEADER}")
    base = text[: idx + 1] if idx >= 0 else text
    if not base.endswith("\n"):
        base += "\n"
    return base.rstrip("\n") + "\n\n" + section


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("notes", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true", help="append/replace the 검증 메모 section in each note")
    args = parser.parse_args(argv)

    today = date.today().isoformat()
    bad = 0
    for path in args.notes:
        text = path.read_text(encoding="utf-8")
        findings = verify_text(text)
        section = render_section(findings, today)
        bad += sum(1 for f in findings if f.exists is False)
        print(f"# {path}\n{section}")
        if args.write:
            path.write_text(upsert_section(text, section), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
