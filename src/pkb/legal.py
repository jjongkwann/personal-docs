"""Explicit legal-source identity and validity metadata; never infer missing dates."""

import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urlsplit

LEGAL_METADATA_FIELDS = (
    "legal_kind", "law_id", "article_id", "case_id", "legal_version",
    "effective_from", "effective_to", "decision_date", "original_url", "original_location",
)
LEGAL_DATE_FIELDS = frozenset({"effective_from", "effective_to", "decision_date"})
LEGAL_IDENTITY_FIELDS = (
    "legal_kind", "law_id", "article_id", "case_id", "legal_version", "effective_from", "decision_date",
)
LEGAL_MAPPING_PROPERTIES = {
    field: {"type": "date", "format": "strict_date"} if field in LEGAL_DATE_FIELDS
    else {"type": "keyword"}
    for field in LEGAL_METADATA_FIELDS
}


def normalize_legal_metadata(frontmatter: dict) -> dict:
    """Keep missing fields absent and explicit nulls clearable in metadata deltas."""
    metadata = {}
    for field in LEGAL_METADATA_FIELDS:
        if field not in frontmatter:
            continue
        value = frontmatter[field]
        if value is None:
            metadata[field] = None
            continue
        if field in LEGAL_DATE_FIELDS:
            # YAML loads unquoted ISO dates as date objects. Datetimes carry an
            # unrequested time/zone; do not silently truncate them to a date.
            if isinstance(value, date) and not isinstance(value, datetime):
                value = value.isoformat()
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{field} must be YYYY-MM-DD or null")
            date.fromisoformat(value)
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a nonempty string or null")
            value = unicodedata.normalize("NFC", value.strip())
        metadata[field] = value
    validate_legal_metadata(metadata)
    return metadata


def validate_legal_metadata(metadata: dict) -> None:
    """Validate identities and intervals, including metadata inherited from ES."""
    # Original-source provenance also occurs on ordinary technical documents.
    # Identity/date/version fields opt a document into the legal contract.
    identity_fields = set(LEGAL_METADATA_FIELDS) - {"original_url", "original_location"}
    if not any(metadata.get(field) is not None for field in identity_fields):
        url = metadata.get("original_url")
        if url is not None and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc):
            raise ValueError("original_url must be an absolute HTTP(S) URL")
        return
    kind = metadata.get("legal_kind")
    if kind not in {"statute", "judgment"}:
        raise ValueError("legal_kind must be statute or judgment when legal metadata is present")
    required_id = "law_id" if kind == "statute" else "case_id"
    if not metadata.get(required_id):
        raise ValueError(f"{required_id} is required for {kind}")
    for field in LEGAL_DATE_FIELDS:
        value = metadata.get(field)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{field} must be YYYY-MM-DD or null")
            date.fromisoformat(value)
    start, end = metadata.get("effective_from"), metadata.get("effective_to")
    if end is not None and (start is None or end <= start):
        raise ValueError("effective_to is exclusive and must be after effective_from")
    url = metadata.get("original_url")
    if url is not None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("original_url must be an absolute HTTP(S) URL")


def preserve_legal_revision(existing: dict[int, dict], incoming: dict) -> None:
    """A new identified revision must not replace the historical path in ES."""
    for old in existing.values():
        for field in LEGAL_IDENTITY_FIELDS:
            before, after = old.get(field), incoming.get(field)
            if before is not None and after is not None and before != after:
                raise ValueError(f"new legal revision changes {field}; use a separate source path to preserve history")
