"""Transparent question planning; extraction never invents legal identifiers."""

import re
import unicodedata
from datetime import date

_DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"
_AS_OF = re.compile(rf"(?:기준일\s*[:：]?\s*|as\s+of\s+)({_DATE})|({_DATE})\s*(?:기준|현재)", re.I)
_ARTICLE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
_CASE = re.compile(
    r"(?<!\w)(\d{4}\s*(?:구합|구단|가합|가단|고합|고단|고정|헌가|헌나|헌다|헌라|헌마|헌바|헌사|헌아"
    r"|다|도|두|드|므|마|바|사|아|누|노|나)\s*\d+)(?!\d)"
)
LEGAL_FILTER_FIELDS = ("law_id", "article_id", "case_id", "legal_version", "legal_kind")


def normalize_as_of(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("as_of는 YYYY-MM-DD 형식이어야 합니다")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("as_of는 유효한 날짜여야 합니다") from exc


def _texts(values: list[str] | None, name: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{name}는 문자열 목록이어야 합니다")
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def normalize_legal_filters(**values: str | None) -> dict[str, str]:
    """Validate exact metadata filters before embedding or issuing ES requests."""
    result = {}
    for field in LEGAL_FILTER_FIELDS:
        value = values.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}는 비어 있지 않은 문자열이어야 합니다")
        result[field] = unicodedata.normalize("NFC", value.strip())
    if result.get("legal_kind") not in {None, "statute", "judgment"}:
        raise ValueError("legal_kind는 statute 또는 judgment이어야 합니다")
    if values.get("as_of") is not None:
        result["as_of"] = normalize_as_of(values["as_of"])
    return result


def analyze_query(
    question: str,
    *,
    issues: list[str] | None = None,
    as_of: str | None = None,
    needed_sources: list[str] | None = None,
    variants: list[str] | None = None,
    category: str | None = None,
    law_id: str | None = None,
    article_id: str | None = None,
    case_id: str | None = None,
    legal_version: str | None = None,
    legal_kind: str | None = None,
) -> dict:
    """Return inspectable issues, source requirements, variants and exact filters.

    Explicit inputs take precedence. Dates require an explicit 기준/현재/as of
    marker; several dates or identifiers are preserved in text rather than
    collapsed into an unsafe global filter. This is deterministic extraction,
    not a claim to resolve the legal meaning of a question. An inferred date
    becomes a search filter only for legal questions; other dates remain in
    the plan because the index has no general historical-validity contract.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("질문은 비어 있지 않은 문자열이어야 합니다")
    question = question.strip()
    filters = normalize_legal_filters(
        as_of=as_of, law_id=law_id, article_id=article_id, case_id=case_id,
        legal_version=legal_version, legal_kind=legal_kind,
    )
    if category:
        filters["category"] = category
    warnings = []
    reference_date = filters.get("as_of")
    matches = list(_AS_OF.finditer(question))
    if as_of is None and matches:
        dates = []
        for match in matches:
            value = next(group for group in match.groups() if group)
            if "년" in value:
                year, month, day = re.findall(r"\d+", value)
                value = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            dates.append(normalize_as_of(value))
        if len(set(dates)) == 1:
            reference_date = dates[0]
        else:
            warnings.append("여러 기준일이 있어 단일 as_of 필터를 적용하지 않았습니다")

    for field, found in (
        ("article_id", [f"제{m[0]}조" + (f"의{m[1]}" if m[1] else "") for m in _ARTICLE.findall(question)]),
        ("case_id", [re.sub(r"\s+", "", value) for value in _CASE.findall(question)]),
    ):
        unique = list(dict.fromkeys(found))
        if field not in filters and len(unique) == 1:
            filters[field] = unique[0]
        elif field not in filters and len(unique) > 1:
            warnings.append(f"여러 {field}가 있어 단일 식별자 필터를 적용하지 않았습니다")

    source_types = _texts(needed_sources, "needed_sources")
    if needed_sources is None:
        if re.search(r"법령|법률|조문|제\s*\d+\s*조", question):
            source_types.append("statute")
        if re.search(r"판결|판례|결정례", question) or filters.get("case_id"):
            source_types.append("judgment")
    if "legal_kind" not in filters and len(source_types) == 1 and source_types[0] in {"statute", "judgment"}:
        filters["legal_kind"] = source_types[0]
    # A mentioned provision is often context for a judgment, and a mentioned
    # judgment context for a statute. Only caller-supplied identifiers may
    # constrain the other kind; inferred identifiers stay in the search text.
    selected_kind = filters.get("legal_kind")
    incompatible = {
        "judgment": [("article_id", article_id)], "statute": [("case_id", case_id)],
    }.get(selected_kind, [])
    if selected_kind is None and {"statute", "judgment"}.issubset(source_types):
        incompatible = [("article_id", article_id), ("case_id", case_id)]
    for field, explicit in incompatible:
        if explicit is None and field in filters:
            del filters[field]
            warnings.append(f"요청한 자료 유형을 찾기 위해 {field}는 검색어에 보존했습니다")
    if as_of is None and reference_date:
        legal_intent = any(field in filters for field in LEGAL_FILTER_FIELDS) or bool(
            {"statute", "judgment"}.intersection(source_types)
        )
        if legal_intent:
            filters["as_of"] = reference_date
        else:
            warnings.append("법률 자료 외에는 기준일 유효성 메타데이터가 없어 날짜 필터를 적용하지 않았습니다")
    issue_list = _texts(issues, "issues")
    if issues is None:
        issue_text = _AS_OF.sub("", question).strip(" ,:：") if len(matches) == 1 else question
        issue_list = [part.strip(" ,:：") for part in re.split(r"[?？;；\n]+", issue_text) if part.strip(" ,:：")]
    if not issue_list:
        issue_list = [question]
    query_variants = _texts(variants, "variants")
    # An issue that only drops the question mark repeats the original search.
    original = question.rstrip("?？ ")
    for issue in issue_list:
        if issue.rstrip("?？ ") != original and issue not in query_variants:
            query_variants.append(issue)
    for source in source_types:
        source_terms = {"statute": "법령 조문", "judgment": "판결 판례"}.get(source, source)
        variant = f"{issue_list[0]} {source_terms}"
        if variant != question and variant not in query_variants:
            query_variants.append(variant)
    return {
        "question": question,
        "issues": issue_list,
        "as_of": reference_date,
        "needed_sources": source_types,
        "variants": query_variants[:3],
        "filters": filters,
        "warnings": warnings,
    }
