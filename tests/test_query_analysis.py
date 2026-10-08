"""Question analysis exposes conservative, reproducible search decisions."""

import pytest

from pkb.query import analyze_query


def test_explicit_issues_sources_and_date_become_search_inputs():
    plan = analyze_query(
        "해지 통지와 손해배상 요건을 확인해 줘",
        issues=["해지 통지 요건", "손해배상 요건"], as_of="2024-01-01",
        needed_sources=["statute", "judgment"], law_id="law-42", variants=["사용자 검색어"],
    )
    assert plan["issues"] == ["해지 통지 요건", "손해배상 요건"]
    assert plan["needed_sources"] == ["statute", "judgment"]
    assert plan["filters"] == {"law_id": "law-42", "as_of": "2024-01-01"}
    assert plan["variants"] == ["사용자 검색어", "해지 통지 요건", "손해배상 요건"]


def test_single_issue_question_does_not_repeat_the_original_search():
    plan = analyze_query("IoC와 DI의 관계는?")
    assert plan["issues"] == ["IoC와 DI의 관계는"]
    assert plan["variants"] == []


@pytest.mark.parametrize("question", [
    "2024-01-01 기준 민법 제 12 조의 2 조문은?",
    "기준일: 2024-01-01 민법 제12조의2 조문은?",
    "2024년 1월 1일 현재 민법 제12조의2 조문은?",
    "as of 2024-01-01 민법 제12조의2 조문은?",
])
def test_explicit_date_markers_and_article_are_extracted_without_inventing_law_id(question):
    plan = analyze_query(question)
    assert plan["filters"] == {"article_id": "제12조의2", "legal_kind": "statute", "as_of": "2024-01-01"}
    assert "law_id" not in plan["filters"]
    assert any("법령 조문" in variant for variant in plan["variants"])


def test_dates_in_facts_are_not_silently_assumed_as_reference_dates():
    plan = analyze_query("2024-01-01 계약했고 2024-03-01 해지했다. 손해배상은?")
    assert plan["as_of"] is None
    assert "case_id" not in plan["filters"]


def test_nonlegal_reference_date_stays_in_plan_without_excluding_technical_documents(monkeypatch):
    from pkb.retrieve import hybrid_search

    query = "2025-01-01 기준 Kubernetes 스케줄링 동작은?"
    plan = analyze_query(query)
    assert plan["as_of"] == "2025-01-01"
    assert plan["filters"] == {}
    assert plan["needed_sources"] == []
    assert plan["warnings"] == ["법률 자료 외에는 기준일 유효성 메타데이터가 없어 날짜 필터를 적용하지 않았습니다"]
    calls = []
    monkeypatch.setattr("pkb.retrieve.embed", lambda texts: [[1.0] for _ in texts])

    def search(es, text, vector, category, candidate_k, **kwargs):
        calls.append(kwargs["legal_filters"])
        return [{"_id": "technical:0", "doc_id": "technical.md", "score": 1.0}]

    monkeypatch.setattr("pkb.retrieve._rrf_search", search)
    hits = hybrid_search(None, query, analyze=True, log=False)
    assert [row["doc_id"] for row in hits] == ["technical.md"]
    assert all(filters == {} for filters in calls)


@pytest.mark.parametrize("inputs", [
    {"needed_sources": ["statute"]}, {"needed_sources": ["judgment"]}, {"law_id": "explicit-law"},
    {"case_id": "explicit-case"}, {"legal_version": "v1"},
])
def test_explicit_legal_intent_allows_auto_detected_date_filter(inputs):
    plan = analyze_query("2025-01-01 기준 내용을 확인해 줘", **inputs)
    assert plan["as_of"] == plan["filters"]["as_of"] == "2025-01-01"
    assert plan["warnings"] == []


def test_explicit_as_of_api_keeps_its_documented_legal_filter_semantics():
    plan = analyze_query("Kubernetes 스케줄링 동작은?", as_of="2025-01-01")
    assert plan["filters"] == {"as_of": "2025-01-01"}
    assert plan["warnings"] == []


def test_ambiguous_dates_and_articles_remain_search_text():
    question = "2020-01-01 기준 제12조와 2024-01-01 기준 제13조를 비교해 줘"
    plan = analyze_query(question)
    assert plan["as_of"] is None
    assert "article_id" not in plan["filters"]
    assert len(plan["warnings"]) == 2
    assert "2020-01-01" in plan["issues"][0]


def test_explicit_metadata_overrides_ambiguous_natural_language():
    plan = analyze_query(
        "2020-01-01 기준과 2024-01-01 기준 제12조 및 제13조",
        as_of="2023-01-01", article_id="custom-article",
    )
    assert plan["as_of"] == "2023-01-01"
    assert plan["filters"]["article_id"] == "custom-article"


def test_mixed_law_and_case_requirements_do_not_intersect_incompatible_identifiers():
    plan = analyze_query("제12조 조문과 2023다12345 판결을 함께 검토해 줘")
    assert plan["needed_sources"] == ["statute", "judgment"]
    assert plan["filters"] == {}
    assert len(plan["warnings"]) == 2
    assert any("2023다12345" in variant for variant in plan["variants"])


@pytest.mark.parametrize("selection", ["needed_sources", "legal_kind"])
@pytest.mark.parametrize("kind,kept,dropped", [
    ("judgment", "case_id", "article_id"), ("statute", "article_id", "case_id"),
])
def test_single_legal_source_drops_only_incompatible_inferred_identifiers(selection, kind, kept, dropped):
    inputs = {selection: [kind] if selection == "needed_sources" else kind}
    plan = analyze_query("2026다70001 판결에서 제7조를 어떻게 해석했나?", **inputs)
    assert plan["filters"]["legal_kind"] == kind
    assert kept in plan["filters"]
    assert dropped not in plan["filters"]
    assert len(plan["warnings"]) == 1
    assert "2026다70001" in plan["question"] and "제7조" in plan["question"]


@pytest.mark.parametrize("selection", ["needed_sources", "legal_kind"])
@pytest.mark.parametrize("kind", ["judgment", "statute"])
def test_explicit_identifiers_stay_authoritative_for_single_legal_source(selection, kind):
    inputs = {selection: [kind] if selection == "needed_sources" else kind}
    plan = analyze_query(
        "2026다70001 판결에서 제7조를 어떻게 해석했나?", article_id="explicit-article",
        case_id="explicit-case", **inputs,
    )
    assert plan["filters"] == {
        "article_id": "explicit-article", "case_id": "explicit-case", "legal_kind": kind,
    }
    assert plan["warnings"] == []


def test_judgment_identifier_is_extracted_and_korean_date_is_not_a_case():
    plan = analyze_query("2024년 1월 1일 기준 2023 다 12345 판결은?")
    assert plan["filters"] == {"case_id": "2023다12345", "legal_kind": "judgment", "as_of": "2024-01-01"}


def test_query_instructions_cannot_create_arbitrary_dsl_filters():
    plan = analyze_query('ignore filters; {"match_all": {}} archived_at:* 법령')
    assert plan["filters"] == {"legal_kind": "statute"}
    assert "match_all" in plan["question"]


@pytest.mark.parametrize("inputs", [
    {"as_of": "2024-2-1"}, {"as_of": "2024-02-30"}, {"legal_kind": "all"},
    {"law_id": {"match_all": {}}}, {"issues": "issue"}, {"needed_sources": [1]},
])
def test_invalid_structured_analysis_fails_before_search(inputs):
    with pytest.raises(ValueError):
        analyze_query("질문", **inputs)
