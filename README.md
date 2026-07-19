# PKB - Personal Knowledge Base

**로컬 저장소와 로컬 검색 인덱스**를 중심으로 돌아가는 개인 지식 베이스. 내가 큐레이션한 문서만을 소스로 사용해, 외부 웹 검색 없이 **통제된 데이터**에서 정보를 빠르게 꺼내 씁니다.

**두 가지 방식으로 씁니다** — ① Claude Code가 **MCP**로 대화 중 이 데이터를 바로 검색/참조/작성 ② 사람이 **Obsidian**에서 코퍼스 원본과 개념노트를 마크다운으로 직접 열람. 문서 원본과 Elasticsearch/SQLite 인덱스는 로컬에 두고, CLI는 운영·검증·대체 인터페이스로만 사용합니다. 100% 로컬 — 코드 안에 LLM API 호출이 없고 `ANTHROPIC_API_KEY`도 필요 없습니다.

주요 용도:
- "내가 예전에 정리한 X 내용이 뭐였지?" 즉시 조회
- 공부 자료, 경력 문서 등 코퍼스 전체 검색
- 검색 결과를 바탕으로 새 마크다운 작성 후 자동 인제스트
- 개념 그래프를 이용해 자료 전체의 관계/로드맵 탐색

## 아키텍처 (요약)

네 개의 층으로 구성됩니다:

1. **데이터 코퍼스** — 개인 문서 원본 저장소 (Source of Truth). 위치는 `DATA_ROOT`(기본 `data/`)로 지정하며, Obsidian 볼트 하위로 두면 원본을 Obsidian과 공유
2. **Elasticsearch** — 청크 검색 엔진 (nori 한국어 분석 + dense_vector kNN)
3. **SQLite Graph DB** — 개념/관계 그래프 (Graph RAG 보조)
4. **MCP 서버** — Claude Code에서 도구로 바로 접근하는 기본 인터페이스

```
[인제스트]
  data/의 문서(md/pdf/docx) → frontmatter 파싱 → 계층적 청킹 → 임베딩 → Elasticsearch 저장

[그래프]
  ES 청크 → Claude Code가 직접 개념·관계 추출 → data/.graph/pkb_graph.sqlite 저장
    → data/_concepts/<slug>.md 노트로 투영

[대화 (Claude Code + MCP)]
  Claude Code 대화 메시지
    ↓
  PKB MCP 도구 호출
    ├─ search_knowledge  → BM25 + kNN + RRF 결합 + CrossEncoder 재순위
    ├─ write_file        → data/ 파일 작성 (자동 인제스트)
    ├─ list_documents    → 저장된 문서 목록
    ├─ add_document      → 문서 인제스트
    ├─ convert_and_ingest → PDF/DOCX → .md 변환 + 인제스트
    ├─ sync_corpus / sync_obsidian / reindex_document / doctor
    └─ graph_list_chunks / graph_store_concepts / sync_concept_notes
```

상세 구조는 [docs/architecture.md](docs/architecture.md)를 참조하세요.

## 시작하기 (MCP 우선)

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

### 2. MCP 서버 기동 + 클라이언트 등록

PKB MCP 서버는 **127.0.0.1에 뜬 단일 HTTP 서버**이고, 모든 에이전트가 그 하나를 공유합니다.

> **stdio로 등록하지 마세요.** stdio는 세션마다 서버 프로세스를 새로 띄우는데, PKB 서버는 임베딩·리랭커
> 모델 때문에 프로세스당 약 4GB를 씁니다. 세션을 여러 개 열거나 에이전트를 팬아웃하면 세션 수 × 4GB로
> 머신이 스왑에 빠집니다. HTTP 공유 서버는 모델을 한 벌만 올립니다.

서버는 launchd로 상시 띄웁니다 (plist 전문은 [docs/mcp.md](docs/mcp.md)). 그다음 클라이언트를 붙입니다:

```bash
claude mcp add --transport http pkb http://127.0.0.1:8787/mcp -s user
codex  mcp add pkb --url http://127.0.0.1:8787/mcp
gemini mcp add pkb http://127.0.0.1:8787/mcp -t http -s user
```

Claude Desktop만 HTTP를 직접 못 붙어서 `mcp-remote` 브리지를 끼웁니다 (자세한 건 [docs/mcp.md](docs/mcp.md)).

`uv run pkb doctor`로 서버 상태(LISTEN·PID·메모리·재기동 횟수)와 ES·그래프를 한 번에 확인합니다.

### 3. 데이터 추가

데이터 코퍼스(`DATA_ROOT`, 기본 `data/`) 하위에 문서를 넣습니다. **최상위 폴더명이 곧 카테고리**이며 동적입니다 — 폴더를 새로 만들면 그게 새 카테고리입니다. doc_id는 위치와 무관하게 항상 `data/<카테고리>/...` 형식입니다:

```
<DATA_ROOT>/
├── study/       # 공부 노트, 논문
├── career/      # 경력, 기술 스택, 프로젝트
├── writing/     # 초안, 노트
├── about/       # 자기소개, 관심사
└── ...          # 폴더 추가 = 카테고리 추가 (예: news, journal, 운동)
```

`DATA_ROOT`를 Obsidian 볼트 하위 폴더(예: `<vault>/PKB`)로 지정하는 것을 권장합니다 — 코퍼스 자체가 볼트 안에 있어 Obsidian으로 바로 열람·편집·백링크할 수 있고, `write_file`/`sync_concept_notes` 산출물도 볼트에 그대로 나타납니다.

Claude Code 대화 중 바로 인제스트 요청 가능:

> "`~/Downloads/paper.pdf`를 study 카테고리로 넣어줘"
> → `convert_and_ingest` 도구가 자동 호출

**볼트 안 코퍼스 밖에 파일을 둔 경우** (선택): `.env`에 `OBSIDIAN_PATH=/path/to/vault`를 설정하면, `DATA_ROOT` 서브트리를 제외한 볼트 나머지를 `category=obsidian`으로 별도 크롤합니다.

```bash
uv run pkb init    # ES 인덱스 생성 (+ OBSIDIAN_PATH 설정 시 초기 인제스트)
```

이후 `uv run pkb sync`로 일괄 재조정(업서트 + 삭제된 파일 정리)할 수 있고, `OBSIDIAN_PATH`를 제거하면 같은 명령이 잔존 문서 정리를 제안합니다.

### 4. 사용

Claude Code에서 자연스럽게 대화:

- *"내 study 자료 중 BM25 관련 내용 찾아줘"*
- *"저장된 문서 목록 보여줘"*
- *"방금 찾은 내용 요약해서 `data/writing/summary.md`에 저장해줘"*
- *"DI, IoC, Bean, Container 개념이 어떻게 연결돼 있어?"*

운영/검증용 CLI (보조):

```bash
# 검색 (RRF + 리랭커)
uv run pkb query "DI IoC 의존성 주입" --category study

# 주변 청크를 함께 확인
uv run pkb query "RAG 검색 품질 개선" --category study --expand 1

# 코퍼스 재조정 (업서트 + 유령 문서 정리)
uv run pkb sync

# 매핑 변경 후 전체 재인덱싱
uv run pkb reindex

# 문서 생명주기 (soft delete)
uv run pkb archive data/career/old_resume.md --reason outdated
uv run pkb restore data/career/old_resume.md
uv run pkb purge-archived --before 2024-01-01   # 비가역 물리 삭제

# SQLite 개념 그래프
uv run pkb graph stats
uv run pkb graph sync-notes
```

## MCP 도구

18개 도구를 제공합니다 (검색 1 · 파일/문서 8 · 생명주기 2 · 상태 1 · Graph RAG 6). 자주 쓰는 것:
`search_knowledge`, `write_file`, `list_documents`, `add_document`, `convert_and_ingest`,
`sync_corpus`, `get_document`, `doctor`. 전체 목록·파라미터는 [docs/mcp.md](docs/mcp.md), 구조는
[docs/architecture.md](docs/architecture.md) 참조.

## 설정

`.env`에서 자주 쓰는 설정:

```env
ES_HOST=http://localhost:9200
ES_INDEX=pkb_documents
DATA_ROOT=/path/to/obsidian-vault/PKB   # 권장: 볼트 하위 폴더
OBSIDIAN_PATH=/path/to/obsidian-vault   # 선택: DATA_ROOT 밖 볼트 파일도 동기화

RERANK_ENABLED=true
CANDIDATE_K=20
EXPAND_CONTEXT=0

GRAPH_DB_PATH=data/.graph/pkb_graph.sqlite
GRAPH_DEDUP_THRESHOLD=0.88
```

`EXPAND_CONTEXT=1`로 설정하면 검색 결과마다 전후 청크가 `neighbors`로 붙습니다.

## 문서

- [MCP 연동 상세](docs/mcp.md) — 등록, 도구 목록, 사용 예시
- [아키텍처 상세](docs/architecture.md) — 데이터 흐름, 구성요소
- [CLI 사용법](docs/usage.md) — 운영·검증·대체 인터페이스
- [Graph RAG 설계](docs/graph-rag.md) — SQLite 기반 개념 그래프 MVP

## 기술 스택

- **MCP** — Claude Code 직접 통합 (기본 사용 방법)
- **Elasticsearch 8.x** — nori 한국어 형태소 분석 + dense_vector kNN
- **sentence-transformers** — 로컬 임베딩 + CrossEncoder 리랭커
- **markitdown** — DOCX/PPTX/XLSX/HTML → 마크다운 변환 (PDF는 pdfminer 페이지 보존 추출)
- **SQLite** — 개념 그래프 저장소 (`data/.graph/pkb_graph.sqlite`)
- **typer** — CLI
- **PyYAML** — 마크다운 frontmatter 파싱
