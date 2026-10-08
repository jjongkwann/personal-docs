"""CLI와 MCP가 같은 출처·토큰 예산으로 검색 근거를 전달한다."""

import json
from functools import lru_cache

import tiktoken


def render_query_plan(plan: dict) -> str:
    return "질문 분석: " + json.dumps(plan, ensure_ascii=False)


@lru_cache(maxsize=1)
def _encoder():
    return tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> list[int]:
    return _encoder().encode(text, disallowed_special=())


def _clip(text: str, limit: int) -> str:
    # decode_bytes는 한국어 토큰 경계에서 생기는 불완전한 UTF-8만 제외한다.
    return _encoder().decode_bytes(_tokens(text)[:limit]).decode("utf-8", errors="ignore")


def render_search_results(
    hits: list[dict],
    *,
    max_tokens: int = 4000,
    hit_concepts: dict | None = None,
    preamble: str = "",
    evidence_out: list[dict] | None = None,
) -> str:
    """매칭 청크를 먼저, 같은 절의 주변 청크를 다음에 전달한다.

    예산은 출처·메타데이터·생략 안내를 포함한 cl100k_base 토큰 수다.
    원본 hits를 바꾸지 않고 (doc_id, chunk_index)가 같은 근거를 한 번만 낸다.
    """
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 256 <= max_tokens <= 32000:
        raise ValueError("문맥 토큰 예산은 256~32000 사이 정수여야 합니다")
    if not hits:
        return _clip("\n\n".join(filter(None, [preamble, "검색 결과가 없습니다."])), max_tokens)

    def key(row):
        return row.get("doc_id") or row.get("source_path", ""), row.get("chunk_index")

    passages: list[tuple[dict, bool]] = []
    seen: set[tuple] = set()
    for row in hits:
        if key(row) not in seen:
            passages.append((row, False))
            seen.add(key(row))
    for row in hits:
        for neighbor in row.get("neighbors", []):
            if row.get("section_path") and neighbor.get("section_path") != row["section_path"]:
                continue
            expanded = {**row, **neighbor}
            if key(expanded) not in seen:
                passages.append((expanded, True))
                seen.add(key(expanded))

    notice = "[문맥 예산으로 일부 근거가 생략되었습니다. 필요한 문서는 get_document로 확인하세요.]"
    budget = max_tokens - len(_tokens("\n\n" + notice))
    parts = [preamble] if preamble else []
    if len(_tokens(preamble)) > budget:
        return _clip(preamble, budget) + "\n\n" + notice
    omitted = False
    for index, (row, context_only) in enumerate(passages, 1):
        header = f"[출처 {index} | doc_id: {row.get('doc_id') or row.get('source_path', '')}"
        if row.get("source_path") and row["source_path"] != row.get("doc_id"):
            header += f" | 경로: {row['source_path']}"
        if row.get("chunk_index") is not None:
            header += f" #{row['chunk_index']}"
        if context_only:
            header += " | 주변 문맥"
        else:
            header += f" | score {row.get('score', 0.0):.3f}"
        for field, label in (
            ("category", "카테고리"), ("title", "제목"), ("doc_type", "유형"),
            ("canonical_id", "정본"), ("status", "상태"),
            ("legal_kind", "법률 자료 유형"), ("law_id", "법령 ID"), ("article_id", "조문"),
            ("case_id", "사건번호"), ("legal_version", "법률 버전"),
            ("effective_from", "시행 시작"), ("effective_to", "시행 종료(미포함)"),
            ("decision_date", "선고일"), ("original_url", "원문 URL"), ("original_location", "원문 위치"),
        ):
            if row.get(field):
                header += f" | {label}: {row[field]}"
        header += "]\n"
        if row.get("section_path"):
            header += f"섹션: {row['section_path']}\n"
        concepts = sorted((hit_concepts or {}).get(key(row), []), key=lambda c: c["slug"])[:5]
        if concepts:
            header += "관련 개념: " + ", ".join(c["name"] for c in concepts) + "\n"
        content = row.get("content", "")
        def record(visible: str, row: dict = row, index: int = index) -> None:
            if evidence_out is not None:
                evidence_out.append({**{k: v for k, v in row.items() if k not in {"embedding", "neighbors"}},
                                     "citation_id": index, "content": visible})

        candidate = "\n\n".join([*parts, header + content])
        if len(_tokens(candidate)) <= budget:
            parts.append(header + content)
            record(content)
            continue
        prefix = "\n\n".join([*parts, header])
        suffix = "\n[본문 일부 생략]"
        available = budget - len(_tokens(prefix + suffix)) - 2
        if available > 0:
            clipped = _clip(content, available)
            while clipped and len(_tokens(prefix + clipped + suffix)) > budget:
                clipped = _clip(clipped, max(0, len(_tokens(clipped)) - 1))
            if clipped:
                parts.append(header + clipped + suffix)
                record(clipped)
        omitted = True
        break
    if omitted:
        parts.append(notice)
    return "\n\n".join(parts)
