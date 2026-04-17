# PKB 아키텍처

PKB는 **MCP-first 개인 지식 베이스**입니다. 기본 사용자는 Claude Code이고, CLI/Web은 운영·검증·대체 인터페이스입니다.

핵심 구성은 네 층입니다:

1. **`data/`와 Obsidian 원본** — 개인 문서의 Source of Truth
2. **Elasticsearch** — 청크 단위 검색 인덱스
3. **SQLite Graph DB** — 개념/관계 그래프
4. **MCP 서버** — Claude Code가 호출하는 기본 작업 인터페이스

LangGraph 기반 CLI/Web 에이전트는 보조 경로입니다. MCP 흐름에서는 Claude Code 자체가 에이전트 역할을 하므로 `src/pkb/agent.py`를 거치지 않습니다.

---

## 전체 다이어그램

```
[사용자]
  ↓
[Claude Code]
  ↓ MCP stdio
[PKB MCP 서버]  src/pkb/mcp_server.py
  ├─ search_knowledge      → Elasticsearch 검색
  ├─ write_file            → data/ 작성 + 자동 인제스트
  ├─ add_document          → data/ 파일 인제스트
  ├─ convert_and_ingest    → 외부 파일 → data/<category>/.md → 인제스트
  ├─ sync_obsidian         → Obsidian 볼트 일괄 인제스트
  ├─ get_document          → 문서 청크/section_path 조회
  ├─ reindex_document      → 단일 문서 재인제스트
  ├─ doctor                → 상태 점검
  └─ graph_*               → SQLite 개념 그래프 조회/저장

[인제스트 파이프라인]  src/pkb/ingest.py
  원본 파일
    → markitdown 변환
    → YAML frontmatter 파싱
    → H1~H3 section_path 기반 계층 청킹
    → sentence-transformers 임베딩
    → Elasticsearch bulk 저장

[검색 파이프라인]  src/pkb/retrieve.py
  질의
    ├─ BM25 검색
    ├─ kNN 벡터 검색
    ├─ RRF 결합
    ├─ CrossEncoder 재순위
    └─ 선택: neighbors(parent context) 부착

[Graph RAG]  src/pkb/graph/
  ES 청크
    → graph_list_chunks로 Claude Code가 읽음
    → Claude Code가 개념/관계 추출
    → graph_store_concepts로 SQLite 저장
    → search_concepts / explain_concept / related_concepts 조회
```

보조 인터페이스:

```
CLI       src/pkb/cli.py    pkb query, pkb reindex, pkb graph stats/build/export
Web UI    src/pkb/web.py    /query, /chat, /
Agent     src/pkb/agent.py  CLI/Web용 LangGraph ReAct 에이전트
```

---

## 1. 원본 저장소

### `data/`

`data/`는 프로젝트 내부 개인 문서 저장소입니다. git에서 추적하지 않는 것을 기본으로 합니다.

```
data/
├── about/      # 자기소개, 관심사
├── career/     # 경력, 기술 스택, 프로젝트
├── study/      # 공부 노트, 교재
├── writing/    # 초안, 노트
├── misc/       # 분류 애매한 자료
├── .logs/      # 검색 로그 JSONL
└── .graph/     # SQLite 그래프 DB
```

지원 포맷:

| 확장자 | 처리 |
|-------|------|
| `.md`, `.markdown`, `.txt` | 직접 읽기 |
| `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html` | `markitdown`으로 마크다운 변환 |

### Obsidian

`.env`의 `OBSIDIAN_PATH`가 설정되어 있으면 외부 Obsidian 볼트를 읽어 ES에 인덱싱합니다.

- `category`: `obsidian`
- `doc_id`: `obsidian/<볼트 상대경로>`
- 원본 파일은 복사하거나 수정하지 않음

---

## 2. Elasticsearch 검색 인덱스

Docker 컨테이너 `pkb-es`로 실행되며, 기본 인덱스는 `pkb_documents`입니다.

### 저장 단위

파일 하나는 여러 청크로 분할되고, 각 청크가 ES 문서 1개로 저장됩니다.

```python
{
    "content": "청크 텍스트...",
    "embedding": [0.1, 0.2, ...],
    "source_path": "data/study/rag/example.md",
    "doc_id": "data/study/rag/example.md",
    "category": "study",
    "chunk_index": 0,
    "section_path": "대주제 > 소주제 > 세부",
    "title": "문서 제목",
    "tags": ["rag", "search"],
    "date_modified": "2026-04-16",
    "language": "ko"
}
```

### 청킹

- YAML frontmatter가 있으면 `title`, `tags` 등 메타데이터를 파싱
- H1~H3 헤딩 경로를 `section_path`로 저장
- 섹션 내부는 기본 500토큰, 100토큰 오버랩으로 분할
- 검색 결과에는 `section_path`가 함께 반환됨

### 검색

`search_knowledge`와 `pkb query`는 같은 검색 파이프라인을 사용합니다.

1. BM25 검색: 한국어 nori 분석기 기반 키워드 검색
2. kNN 검색: sentence-transformers 임베딩 기반 의미 검색
3. RRF 결합: 두 결과 집합의 순위를 Reciprocal Rank Fusion으로 결합
4. CrossEncoder 재순위: 기본 `BAAI/bge-reranker-v2-m3`
5. 선택적 주변 청크: `EXPAND_CONTEXT=N` 또는 CLI `--expand N`

검색 호출은 `data/.logs/search.jsonl`에 JSONL로 기록됩니다.

---

## 3. SQLite Graph DB

Graph RAG는 검색을 대체하지 않고, 개념 간 관계 질의를 보완합니다.

저장 위치:

```text
data/.graph/pkb_graph.sqlite
```

주요 테이블:

| 테이블 | 역할 |
|--------|------|
| `concepts` | 정규화된 개념 노드 |
| `concept_aliases` | DI 같은 별칭 |
| `documents` | ES `doc_id`와 연결되는 문서 노드 |
| `concept_edges` | 개념 간 관계 |
| `concept_mentions` | 개념이 등장한 `doc_id`/`chunk_index` |
| `graph_runs` | CLI 일괄 빌드 실행 기록 |

MCP-first 그래프 구축:

1. Claude Code가 `graph_list_chunks(category="study", limit=20)`로 청크를 읽음
2. Claude Code가 청크 내용에서 개념과 관계를 추출
3. `graph_store_concepts(items_json=...)`로 SQLite에 저장
4. `search_concepts`, `explain_concept`, `related_concepts`로 조회

CLI 일괄 빌드:

```bash
uv run pkb graph build --category study
```

이 경로는 `ANTHROPIC_API_KEY`를 사용해 내부 빌더가 Haiku를 호출합니다. MCP 기본 흐름은 아닙니다.

---

## 4. MCP 서버

`src/pkb/mcp_server.py`가 PKB의 기본 인터페이스입니다.

```
Claude Code → MCP stdio → mcp_server.py → ES / data / SQLite
```

제공 도구:

| 범주 | 도구 |
|------|------|
| 검색 | `search_knowledge` |
| 파일/문서 | `write_file`, `list_documents`, `add_document`, `convert_and_ingest`, `sync_obsidian`, `get_document`, `reindex_document` |
| 상태 | `doctor` |
| Graph RAG | `graph_list_chunks`, `graph_store_concepts`, `search_concepts`, `explain_concept`, `related_concepts` |

MCP 서버가 지키는 경계:

- `write_file`은 `data/` 하위 `.md`만 작성
- `add_document`는 `data/` 하위만 인제스트
- `convert_and_ingest`는 외부 파일을 읽을 수 있지만 결과는 `data/<category>/`에 저장
- Obsidian 동기화는 원본 볼트를 수정하지 않고 ES에만 반영
- 장기 실행 감시(`pkb watch`)는 MCP 도구가 아니라 별도 CLI 프로세스

---

## 5. 보조 인터페이스

### CLI

`src/pkb/cli.py`는 운영과 검증에 사용합니다.

주요 명령:

```bash
uv run pkb init
uv run pkb reindex
uv run pkb query "DI IoC 의존성 주입" --category obsidian --expand 1
uv run pkb graph stats
uv run pkb graph export /tmp/pkb-graph.mmd
```

### Web UI

`uv run pkb serve`로 실행합니다. 검색 결과의 `section_path`와 선택적 `neighbors` 확인에 유용합니다.

### LangGraph 에이전트

`src/pkb/agent.py`는 CLI/Web의 대화형 모드용입니다.

- CLI: `uv run pkb chat`
- Web: `/chat`

MCP 경로에서는 사용하지 않습니다.

---

## 실제 흐름 예시

### 예시 1: 자료 기반 정리 노트 생성

```
사용자: "저장된 BM25 관련 내용 정리해서 data/writing/bm25.md에 저장해줘"
  ↓
Claude Code
  ├─ search_knowledge(query="BM25", category="study")
  ├─ 검색 결과를 읽고 요약 작성
  └─ write_file(file_path="data/writing/bm25.md", content="...")
       └─ 저장 후 자동 인제스트
```

### 예시 2: 외부 PDF 추가

```
사용자: "~/Downloads/paper.pdf를 study 카테고리로 넣어줘"
  ↓
Claude Code
  └─ convert_and_ingest(input_path="~/Downloads/paper.pdf", category="study")
       ├─ markitdown 변환
       ├─ data/study/paper.md 저장
       └─ ES 인제스트
```

### 예시 3: 개념 관계 질의

```
사용자: "DI, IoC, Bean, Container가 어떻게 연결돼 있어?"
  ↓
Claude Code
  ├─ search_concepts("DI IoC Bean Container")
  ├─ explain_concept("Dependency Injection", depth=1)
  ├─ related_concepts("IoC")
  └─ 필요한 경우 get_document로 근거 청크 확인
```

---

## 왜 이 구조인가

| 구성요소 | 역할 | 선택 이유 |
|---------|------|-----------|
| MCP | 기본 인터페이스 | Claude Code가 바로 개인 지식 도구를 호출할 수 있음 |
| `data/` | 원본 저장소 | 사람이 읽고 편집 가능한 단일 원본 |
| Elasticsearch | 검색 인덱스 | 한국어 키워드 검색과 벡터 검색을 함께 운용 |
| RRF + 리랭커 | 검색 품질 | 키워드/의미 검색의 장점을 결합하고 최종 정밀도 보정 |
| SQLite Graph DB | 개념 관계 저장 | 개인 규모에서 설치/운영 부담이 작고 백업 쉬움 |
| CLI/Web | 보조 인터페이스 | 재인덱싱, 디버깅, 검색 품질 확인에 적합 |

---

## 교체 가능 지점

- **Elasticsearch → Qdrant/Chroma/pgvector**: `store.py`, `retrieve.py` 교체
- **sentence-transformers → 다른 임베딩**: `embeddings.py` 교체
- **SQLite Graph DB → Neo4j**: `src/pkb/graph/store.py` 계층 교체
- **markitdown → docling/unstructured**: `ingest.py`의 파일 읽기 경로 교체
- **Claude Code MCP → 다른 MCP 클라이언트**: `mcp_server.py`는 그대로 사용 가능
