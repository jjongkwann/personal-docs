from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from pkb.config import settings
from pkb.tools import list_documents, search_knowledge, write_file

SYSTEM_PROMPT = """당신은 개인 지식 관리 어시스턴트입니다.
사용자의 개인 데이터(경력, 공부 노트, 자기소개 등)가 Elasticsearch에 저장되어 있습니다.

## 도구 사용 가이드

- 사용자의 질문에 답하려면 **search_knowledge** 도구로 관련 정보를 먼저 검색하세요.
- 파일 작성 요청 시 관련 정보를 검색한 뒤 **write_file** 도구로 작성하세요.
- 저장된 문서를 확인하려면 **list_documents**를 사용하세요.
- 검색 결과가 부족하면 다른 키워드나 카테고리로 재검색하세요.
- 검색 없이 답할 수 있는 일반 질문은 도구 없이 직접 답하세요.

## 카테고리
- about: 자기소개, 관심사
- career: 경력, 기술 스택, 프로젝트
- study: 공부 노트, 학습 자료
- writing: 블로그 아이디어, 초안

## 답변 스타일
- 한국어로 답변하세요.
- 검색 결과를 기반으로 답변할 때는 출처를 언급하세요.
- 파일을 작성할 때는 마크다운 형식을 사용하세요.
"""


def create_agent():
    """LangGraph ReAct 에이전트 생성."""
    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=settings.anthropic_api_key,
        max_tokens=4096,
    )
    tools = [search_knowledge, write_file, list_documents]
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)


def chat(agent, message: str, history: list[dict]) -> tuple[str, list[dict]]:
    """대화 메시지 처리. 히스토리 유지.

    Returns:
        (응답 텍스트, 업데이트된 히스토리)
    """
    messages = []
    for h in history:
        if h["role"] == "human":
            messages.append(HumanMessage(content=h["content"]))
        else:
            messages.append(AIMessage(content=h["content"]))
    messages.append(HumanMessage(content=message))

    result = agent.invoke({"messages": messages})

    response = result["messages"][-1].content
    # 응답이 리스트인 경우 (content blocks) 텍스트만 추출
    if isinstance(response, list):
        response = "\n".join(
            block["text"] for block in response if block.get("type") == "text"
        )

    history = history + [
        {"role": "human", "content": message},
        {"role": "assistant", "content": response},
    ]

    return response, history
