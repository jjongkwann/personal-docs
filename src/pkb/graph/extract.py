"""Claude Haiku 기반 개념·관계 추출."""

import json
import re
from pathlib import Path

from pkb.config import settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.txt"
_prompt_template: str | None = None
_llm = None


def _get_prompt() -> str:
    global _prompt_template
    if _prompt_template is None:
        _prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_template


def _get_llm():
    global _llm
    if _llm is None:
        from langchain_anthropic import ChatAnthropic

        _llm = ChatAnthropic(
            model=settings.graph_extract_model,
            api_key=settings.anthropic_api_key,
            max_tokens=1500,
        )
    return _llm


def _extract_json(text: str) -> dict | None:
    """LLM 응답에서 JSON 객체 하나 뽑기. 코드블록 래핑/앞뒤 텍스트 허용."""
    if not text:
        return None
    # 코드블록 제거
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 첫 { 부터 마지막 } 까지
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class ExtractionError(Exception):
    """추출 실패. API 크레딧/네트워크 등 치명적 오류에 발생."""


def extract_from_chunk(chunk_content: str, raise_on_api_error: bool = True) -> tuple[list[dict], list[dict]]:
    """청크에서 개념/관계 추출. (concepts, relations) 반환.

    Args:
        raise_on_api_error: True면 API 에러(크레딧/인증 등) 시 ExtractionError 발생.
                            False면 ([],[]) 반환.
    """
    if not chunk_content.strip():
        return [], []

    # 청크가 너무 길면 앞부분만 (프롬프트+응답 토큰 한도 관리)
    if len(chunk_content) > 4000:
        chunk_content = chunk_content[:4000]

    prompt = _get_prompt().replace("{chunk_content}", chunk_content)

    try:
        response = _get_llm().invoke(prompt)
    except Exception as e:
        if raise_on_api_error:
            raise ExtractionError(f"LLM 호출 실패: {e}") from e
        return [], []

    try:
        text = response.content if isinstance(response.content, str) else str(response.content)
        data = _extract_json(text)
        if not data or not isinstance(data, dict):
            return [], []
        concepts = data.get("concepts", [])
        relations = data.get("relations", [])
        concepts = [
            c for c in concepts
            if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()
        ]
        relations = [
            r for r in relations
            if isinstance(r, dict) and all(
                isinstance(r.get(k), str) and r[k].strip() for k in ("src", "dst", "type")
            )
        ]
        return concepts, relations
    except Exception:
        # JSON 파싱 실패 등은 스킵 (개별 청크 불량)
        return [], []
