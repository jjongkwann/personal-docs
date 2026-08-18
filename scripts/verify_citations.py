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
SECTION_HEADER = "## 인용 검증 메모"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
USER_AGENT = "pkb-verify-citations/1 (+https://github.com/jjongkwann/personal-docs)"


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
    exists: bool
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
        return exc.code, b""


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
            raise SystemExit(f"arXiv API HTTP {status}")
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


def verify_text(text: str, *, arxiv_fetch=fetch_arxiv, cve_fetch=fetch_cve) -> list[Finding]:
    findings: list[Finding] = []
    arxiv_ids = extract_arxiv_ids(text)
    metas = arxiv_fetch(arxiv_ids) if arxiv_ids else {}
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
    return findings


def render_section(findings: list[Finding], today: str) -> str:
    lines = [
        SECTION_HEADER,
        "",
        f"자동 검증 {today} (`scripts/verify_citations.py`, arXiv API·MITRE CVE). "
        "venue 미확인 = arXiv 메타(comment/journal_ref)에 근거 없음 → 확인 전까지 'arXiv preprint'로 취급. "
        "제목·수치·저자 대조는 사람이 한다.",
        "",
    ]
    if not findings:
        lines.append("- 검증 대상 인용(arXiv/CVE ID) 없음.")
    for f in findings:
        mark = "✅" if f.exists else "❌"
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
        bad += sum(1 for f in findings if not f.exists)
        print(f"# {path}\n{section}")
        if args.write:
            path.write_text(upsert_section(text, section), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
