# PKB - Personal Knowledge Base

**로컬 저장소와 로컬 검색 인덱스**를 중심으로 돌아가는 개인 지식 베이스. 내가 큐레이션한 문서만을 소스로 사용해, 외부 웹 검색 없이 **통제된 데이터**에서 정보를 빠르게 꺼내 씁니다.

Claude Code에 **MCP**로 연결해서 대화 중 이 데이터를 바로 검색/참조/작성하는 것이 기본 사용 방식입니다. 문서 원본과 Elasticsearch/SQLite 인덱스는 로컬에 두고, CLI는 운영·검증·대체 인터페이스로만 사용합니다.

주요 용도:
- "내가 예전에 정리한 X 내용이 뭐였지?" 즉시 조회
- Obsidian 노트, 자료, 경력 문서 검색
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
    → data/concepts/<slug>.md 노트로 투영

[대화 (Claude Code + MCP)]
  Claude Code 대화 메시지
    ↓
  PKB MCP 도구 호출
    ├─ search_knowledge  → BM25 + kNN + RRF 결합 + CrossEncoder 재순위
    ├─ write_file        → data/ 파일 작성 (자동 인제스트)
    ├─ list_documents    → 저장된 문서 목록
    ├─ add_document      → 문서 인제스트
    ├─ convert_and_ingest → PDF/DOCX → .md 변환 + 인제스트
    ├─ sync_obsidian / reindex_document / doctor
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

데이터 코퍼스(`DATA_ROOT`, 기본 `data/`) 하위에 문서를 넣습니다. doc_id는 위치와 무관하게 `data/<카테고리>/...` 형식입니다:

```
<DATA_ROOT>/
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
```

이후 `uv run pkb sync`로 일괄 재조정(업서트 + 삭제된 파일 정리)할 수 있고, `OBSIDIAN_PATH`를 제거하면 같은 명령이 잔존 문서 정리를 제안합니다.

나아가 `DATA_ROOT`를 볼트 하위 폴더(예: `<vault>/PKB`)로 지정하면 코퍼스 원본 자체를 Obsidian과 공유합니다 — Obsidian으로 열람·편집·백링크, PKB로 검색·작성(`write_file` 산출물이 볼트에 바로 나타남). 볼트 크롤은 이 서브트리를 자동 제외하므로 이중 인제스트는 없습니다.

### 4. 사용

Claude Code에서 자연스럽게 대화:

- *"내 study 자료 중 BM25 관련 내용 찾아줘"*
- *"저장된 문서 목록 보여줘"*
- *"방금 찾은 내용 요약해서 `data/writing/summary.md`에 저장해줘"*
- *"DI, IoC, Bean, Container 개념이 어떻게 연결돼 있어?"*

운영/검증용 CLI (보조):

```bash
# 검색 (RRF + 리랭커)
uv run pkb query "DI IoC 의존성 주입" --category obsidian

# 주변 청크를 함께 확인
uv run pkb query "RAG 검색 품질 개선" --category study --expand 1

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

Claude Code에서 사용할 수 있는 주요 도구:

| 도구 | 역할 |
|------|------|
| `search_knowledge` | 개인 지식 베이스 검색 (BM25 + kNN + RRF + 리랭커) |
| `write_file` | `data/` 하위 `.md` 작성 + 기본 자동 인제스트 |
| `list_documents` | ES에 저장된 문서 목록 조회 |
| `add_document` | `data/` 하위 문서 인제스트 |
| `convert_and_ingest` | 외부 PDF/DOCX/PPTX/XLSX/HTML을 `.md`로 변환 후 인제스트 |
| `sync_obsidian` | Obsidian 볼트 재조정 (업서트 + 볼트에 없는 문서 정리) |
| `get_document` | 특정 문서의 청크와 `section_path` 조회 |
| `reindex_document` | 특정 원본 문서 재인제스트 |
| `archive_document` | 문서를 soft delete (검색에서 제외, 복구 가능) |
| `restore_document` | 아카이브된 문서를 복구 |
| `doctor` | ES 연결, 인덱스, 설정 상태 점검 (archived/expired 포함) |
| `graph_list_chunks` | Graph RAG 추출용 청크 페이지 조회 |
| `graph_store_concepts` | Claude Code가 추출한 개념/관계를 SQLite에 저장 |
| `sync_concept_notes` | SQLite 개념그래프를 `data/concepts/<slug>.md` 노트로 투영 |

## 설정

`.env`에서 자주 쓰는 설정:

```env
ES_HOST=http://localhost:9200
ES_INDEX=pkb_documents
OBSIDIAN_PATH=/path/to/obsidian-vault

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
- **markitdown** — PDF/DOCX/PPTX/XLSX/HTML → 마크다운 변환
- **SQLite** — 개념 그래프 저장소 (`data/.graph/pkb_graph.sqlite`)
- **typer** — CLI
- **PyYAML** — 마크다운 frontmatter 파싱
