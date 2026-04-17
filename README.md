# PKB - Personal Knowledge Base

**로컬**에서 돌아가는 개인 지식 베이스. 내가 큐레이션한 문서(경력, 공부 노트, 자료 등)만을 소스로 사용해, 외부 검색 없이 **통제된 데이터**에서 정보를 빠르게 꺼내 씁니다.

Claude Code에 **MCP**로 연결하면 대화 중 이 데이터를 바로 검색/참조/작성할 수 있습니다. 웹 검색이나 일반 문서 업로드가 아니라, **내가 쌓은 자료만** 근거로 답하는 구조.

주요 용도:
- "내가 예전에 정리한 X 내용이 뭐였지?" 즉시 조회
- 외부 유출 없이 완전 로컬에서 처리

## 아키텍처 (요약)

세 개의 층으로 구성됩니다:

1. **`data/`** — 개인 문서 원본 저장소 (Source of Truth)
2. **Elasticsearch** — 검색 엔진 (nori 한국어 분석 + dense_vector kNN)
3. **MCP 서버** — Claude Code에서 도구로 바로 접근

```
[인제스트]
  data/의 문서(md/pdf/docx) → frontmatter 파싱 → 계층적 청킹 → 임베딩 → Elasticsearch 저장

[대화 (Claude Code + MCP)]
  Claude Code 대화 메시지
    ↓
  PKB MCP 도구 호출
    ├─ search_knowledge  → BM25 + kNN + RRF 결합 + CrossEncoder 재순위
    ├─ write_file        → data/ 파일 작성 (자동 인제스트)
    ├─ list_documents    → 저장된 문서 목록
    ├─ add_document      → 문서 인제스트
    └─ convert_and_ingest → PDF/DOCX → .md 변환 + 인제스트
```

상세 구조는 [docs/architecture.md](docs/architecture.md)를 참조하세요.

## 시작하기 (MCP 중심)

### 사전 요구사항

- Python 3.11+
- Docker
- [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)

### 1. 설치 및 ES 시작

```bash
# 레포 클론
git clone <repo-url> personal-docs
cd personal-docs

# ES 컨테이너 빌드 및 실행 (nori 플러그인 포함)
docker compose up -d

# Python 의존성 설치
uv sync

# ES 인덱스 초기화
uv run pkb init
```

### 2. MCP 서버를 Claude Code에 등록

프로젝트 디렉터리에서:

```bash
claude mcp add pkb -s user -- uv --directory "$(pwd)" run python -m pkb.mcp_server
```

또는 `~/.claude.json`의 `mcpServers`에 직접 추가 (경로는 실제 경로로):

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

Claude Code 재시작 후 `/mcp`로 `pkb` 서버가 연결됐는지 확인.

### 3. 데이터 추가

`data/` 하위에 문서를 넣습니다:

```
data/
├── about/       # 자기소개, 관심사
├── career/      # 경력, 기술 스택, 프로젝트
├── study/       # 공부 노트, 교재
└── writing/     # 초안, 노트
```

Claude Code 대화 중 바로 인제스트 요청 가능:

> "`~/Downloads/paper.pdf`를 study 카테고리로 넣어줘"
> → `convert_and_ingest` 도구가 자동 호출

**Obsidian 볼트 연동** (선택):

`.env`에 `OBSIDIAN_PATH=/path/to/vault`를 설정하면:

```bash
uv run pkb init    # ES 인덱스 생성 + Obsidian 볼트 초기 인제스트
uv run pkb watch   # data/와 Obsidian 볼트를 감시, 변경 시 자동 재인제스트
```

Obsidian에서 노트를 편집하면 `pkb watch`가 실시간으로 ES 인덱스를 갱신합니다.

### 4. 사용

Claude Code에서 자연스럽게 대화:

- *"내 study 자료 중 BM25 관련 내용 찾아줘"*
- *"저장된 문서 목록 보여줘"*
- *"방금 찾은 내용 요약해서 `data/writing/summary.md`에 저장해줘"*

## 문서

- [MCP 연동 상세](docs/mcp.md) — 등록, 도구 목록, 사용 예시
- [아키텍처 상세](docs/architecture.md) — 데이터 흐름, 구성요소
- [CLI 사용법](docs/usage.md) — MCP 외 직접 사용(옵션)

## 기술 스택

- **MCP** — Claude Code 직접 통합 (기본 사용 방법)
- **Elasticsearch 8.x** — nori 한국어 형태소 분석 + dense_vector kNN
- **sentence-transformers** — 로컬 임베딩 (`paraphrase-multilingual-MiniLM-L12-v2`, 384차원)
- **markitdown** — PDF/DOCX/PPTX/XLSX/HTML → 마크다운 변환
- **LangGraph + LangChain** — 대화형 에이전트 (CLI/Web 대체 인터페이스용)
- **typer** — CLI / **FastAPI + Jinja2** — Web UI (옵션)
