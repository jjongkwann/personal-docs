"""수집 항목(회의·일정·메일·드라이브 문서) frontmatter 메타데이터.

키가 없는 옛 노트는 청크에도 키를 넣지 않는다 — ingest의 문서 계약 필드와 같은 규칙이다.
명시적 null은 None으로 남겨 메타데이터 델타로 지울 수 있게 한다.
"""

import logging
import re
import unicodedata
from datetime import date, datetime

_log = logging.getLogger(__name__)

ITEM_METADATA_FIELDS = (
    "source_type",
    "occurred_at",
    "people",
    "source_id",
    "source_link",
    "links",
    "attachments",
)
ITEM_LIST_FIELDS = frozenset({"people", "links", "attachments"})
ITEM_MAPPING_PROPERTIES = {
    "source_type": {"type": "keyword"},
    "occurred_at": {"type": "date"},
    "people": {"type": "keyword"},
    "source_id": {"type": "keyword"},
    # 원본 서비스 URL은 보여 주기만 한다 — 검색·필터 대상이 아니다.
    "source_link": {"type": "keyword", "index": False},
    "links": {"type": "keyword"},
    "attachments": {"type": "keyword"},
}

_EMAIL_RE = re.compile(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+")


def normalize_person(value: str) -> str:
    """이메일 주소는 소문자로, 이름은 NFC로 맞춘다. 저장과 검색 필터가 같은 규칙을 쓴다."""
    text = unicodedata.normalize("NFC", str(value).strip())
    return text.lower() if _EMAIL_RE.fullmatch(text) else text


def parse_occurred_at(value: object) -> str | None:
    """occurred_at을 ISO 8601 문자열로. 해석할 수 없으면 None과 경고 로그."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.strip())
        except ValueError:
            _log.warning("invalid occurred_at (ISO parse failed): %r", value)
            return None
        return value.strip()
    _log.warning("unsupported occurred_at type: %s (%r)", type(value).__name__, value)
    return None


def normalize_item_metadata(frontmatter: dict) -> dict:
    """frontmatter에서 항목 메타데이터만 골라 정규화한다.

    목록 필드는 쉼표 구분 문자열도 받고(tags와 같은 규칙) 순서를 지키며 중복을 없앤다.
    source_type은 필터가 대소문자에 흔들리지 않도록 소문자로 둔다. 해석할 수 없는
    occurred_at은 버린다 — 잘못된 날짜가 청크 bulk 전체를 실패시키지 않게 한다.
    """
    metadata: dict = {}
    for field in ITEM_METADATA_FIELDS:
        if field not in frontmatter:
            continue
        value = frontmatter[field]
        if value is None:
            metadata[field] = None
            continue
        if field == "occurred_at":
            parsed = parse_occurred_at(value)
            if parsed is not None:
                metadata[field] = parsed
            continue
        if field in ITEM_LIST_FIELDS:
            if isinstance(value, str):
                values = value.split(",")
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
            else:
                values = [value]
            items = [str(item).strip() for item in values if item is not None and str(item).strip()]
            if field == "people":
                items = [normalize_person(item) for item in items]
            metadata[field] = list(dict.fromkeys(items))
            continue
        text = str(value).strip()
        if field == "source_type":
            text = text.lower()
        metadata[field] = text or None
    return metadata
