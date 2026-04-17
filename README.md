# PKB - Personal Knowledge Base

대화형 에이전틱 RAG 시스템. 개인 데이터를 저장하고, 대화를 통해 질문에 답하거나 블로그/이력서 등을 생성합니다.

## 아키텍처

```
문서(Markdown) → 청킹 → 임베딩 → Elasticsearch 저장
                                        ↓
사용자 대화 → LangGraph 에이전트 (ReAct) → 도구 호출 판단
                ├─ search_knowledge: ES 하이브리드 검색 (BM25 + kNN)
                ├─ write_file: 파일 작성 (블로그, 이력서 등)
                ├─ list_documents: 저장된 문서 목록
                └─ 직접 답변
```

- **LangGraph + LangChain** — 에이전트 루프, 도구 사용, 대화 히스토리
- **Elasticsearch 8.x** — nori 한국어 형태소 분석 + dense_vector kNN
- **sentence-transformers** — 로컬 임베딩 (`paraphrase-multilingual-MiniLM-L12-v2`, 384차원)
- **Claude API** — LLM (langchain-anthropic)
- **typer** — CLI / **FastAPI + Jinja2** — Web UI

## 시작하기

### 사전 요구사항

- Python 3.11+
- Docker
- [uv](https://docs.astral.sh/uv/)
- `ANTHROPIC_API_KEY` (Claude API 키)

### 설치

```bash
# ES 컨테이너 빌드 및 실행 (nori 플러그인 포함)
docker compose up -d

# Python 의존성 설치
uv sync

# 환경 변수 설정
cp .env.example .env
# .env 파일에 ANTHROPIC_API_KEY 입력

# ES 인덱스 초기화
uv run pkb init
```

## 사용법

### 대화형 RAG

```bash
uv run pkb chat
```

```
질문> 내가 공부한 RAG 관련 내용 정리해줘
# → search_knowledge 도구로 study 카테고리 검색 후 답변

질문> 그걸 블로그 포스트로 만들어서 data/writing/rag-blog.md에 저장해줘
# → 검색 + write_file 도구로 파일 생성

질문> 내 경력 정보로 ML 엔지니어 이력서 만들어줘
# → career 카테고리 검색 + 이력서 작성 + 파일 저장
```

에이전트가 자동으로 검색 필요 여부를 판단하고, 적절한 도구를 호출합니다.

### 문서 인제스트

```bash
# 단일 파일
uv run pkb add data/about/bio.md

# 디렉터리 전체 (재귀)
uv run pkb add data/study/

# 태그 지정
uv run pkb add data/career/ --tags "python,backend,fastapi"
```

`data/` 디렉터리 구조:

```
data/
├── about/       # 자기소개, 관심사
├── career/      # 경력, 기술 스택, 프로젝트
├── study/       # 공부 노트, 교재
└── writing/     # 블로그 아이디어, 초안
```

### 단순 검색 (에이전트 없이)

```bash
uv run pkb query "벡터 검색의 원리는?"
uv run pkb query "Python 프레임워크 경험" --category career --top-k 10
```

### 문서 관리

```bash
uv run pkb list
uv run pkb list --category study
uv run pkb delete data/study/rag-overview.md
```

### Web UI

```bash
uv run pkb serve
# http://localhost:8000
```

브라우저에서 문서 목록, 검색, 채팅을 할 수 있습니다.

### Claude Code MCP 연동

Claude Code 어느 세션에서든 PKB MCP 도구를 사용할 수 있습니다.

MCP 도구:
- `search_knowledge` — 하이브리드 검색 (BM25 + kNN)
- `write_file` — 파일 작성 (data/ 하위만 허용)
- `list_documents` — 문서 목록
- `add_document` — 문서 인제스트

`~/.claude.json`의 `mcpServers`에 등록 (경로는 이 레포를 클론한 실제 경로로 교체):

```json
{
  "mcpServers": {
    "pkb": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/personal-docs", "run", "python", "-m", "pkb.mcp_server"]
    }
  }
}
```

또는 프로젝트 디렉터리에서 CLI로 등록:

```bash
claude mcp add pkb -s user -- uv --directory "$(pwd)" run python -m pkb.mcp_server
```

등록 후 Claude Code 재시작 → `/mcp`로 확인.

## 프로젝트 구조

```
├── docker-compose.yml       # ES + nori 컨테이너
├── Dockerfile.es            # ES 이미지 + nori 플러그인
├── pyproject.toml
├── .env.example             # 환경 변수 템플릿
│
├── data/                    # 개인 문서 (gitignored)
│
└── src/pkb/
    ├── mcp_server.py        # MCP 서버 (Claude Code 연동)
    ├── agent.py             # LangGraph ReAct 에이전트
    ├── tools.py             # 에이전트 도구 (search, write, list)
    ├── cli.py               # CLI 커맨드 (chat, add, query, list, delete, serve)
    ├── config.py            # 설정 관리
    ├── ingest.py            # 마크다운 파싱, 청킹
    ├── embeddings.py        # sentence-transformers 임베딩
    ├── store.py             # Elasticsearch CRUD, 인덱스 관리
    ├── retrieve.py          # 하이브리드 검색 (BM25 + kNN)
    ├── web.py               # FastAPI 웹 서버
    └── templates/           # Jinja2 HTML 템플릿
```

## 설정

`.env` 파일:

```
ANTHROPIC_API_KEY=sk-ant-...    # Claude API 키 (필수)
ES_HOST=http://localhost:9200   # Elasticsearch 호스트 (기본값)
ES_INDEX=pkb_documents          # 인덱스 이름 (기본값)
```

## 청킹 전략

- 마크다운 `## Heading` 경계에서 우선 분할
- 고정 크기 500토큰 + 100토큰 오버랩
- 단락(`\n\n`) 경계 존중
