"""ingest.parse_expires_at 정규화 테스트."""

from __future__ import annotations

from datetime import date, datetime

from pkb.ingest import parse_expires_at


def test_none_returns_none():
    assert parse_expires_at(None) is None


def test_date_object_returns_iso():
    # YAML이 '2026-12-31'을 date 객체로 파싱하는 케이스
    assert parse_expires_at(date(2026, 12, 31)) == "2026-12-31"


def test_datetime_object_returns_iso():
    dt = datetime(2026, 12, 31, 10, 30)
    assert parse_expires_at(dt) == "2026-12-31T10:30:00"


def test_iso_string_is_reparsed_and_normalized():
    # 입력 그대로가 아니라 datetime 경유 후 isoformat 재출력
    out = parse_expires_at("2026-12-31")
    assert out is not None
    # 날짜만 있는 ISO 문자열은 datetime.fromisoformat이 자정으로 해석
    assert out.startswith("2026-12-31")


def test_iso_string_with_time():
    out = parse_expires_at("2026-12-31T10:30:00")
    assert out == "2026-12-31T10:30:00"


def test_invalid_string_returns_none():
    assert parse_expires_at("not-a-date") is None
    assert parse_expires_at("2026/12/31") is None  # 슬래시는 ISO 아님


def test_unsupported_type_returns_none():
    assert parse_expires_at(12345) is None
    assert parse_expires_at(["2026-12-31"]) is None
    assert parse_expires_at({"year": 2026}) is None
