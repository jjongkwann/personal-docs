"""Lifecycle 필터 shape 회귀 — archived/expired 문서가 쿼리에서 빠지는 조건."""

from __future__ import annotations

from pkb.retrieve import _lifecycle_filter


def test_include_archived_returns_empty():
    assert _lifecycle_filter(include_archived=True) == []


def test_default_excludes_archived_and_expired():
    filters = _lifecycle_filter(include_archived=False)
    assert len(filters) == 2

    # 1) archived_at 존재 금지
    archived_clause = filters[0]
    assert archived_clause == {"bool": {"must_not": {"exists": {"field": "archived_at"}}}}

    # 2) expires_at 없거나 미래
    expires_clause = filters[1]
    assert "bool" in expires_clause
    shoulds = expires_clause["bool"]["should"]
    assert expires_clause["bool"]["minimum_should_match"] == 1
    assert {"bool": {"must_not": {"exists": {"field": "expires_at"}}}} in shoulds
    assert {"range": {"expires_at": {"gt": "now"}}} in shoulds


def test_filter_uses_now_keyword():
    # ES의 now 키워드 — 애플리케이션 시간 주입 없이 ES가 자체 계산
    filters = _lifecycle_filter(include_archived=False)
    ranges = [s for s in filters[1]["bool"]["should"] if "range" in s]
    assert ranges[0]["range"]["expires_at"]["gt"] == "now"
