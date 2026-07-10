# PKB 아키텍처

PKB는 **MCP-first 개인 지식 베이스**입니다. 기본 사용자는 Claude Code이고, CLI는 운영·검증·대체 인터페이스입니다.

핵심 구성은 네 층입니다:

1. **`data/`와 Obsidian 원본** — 개인 문서의 Source of Truth
2. **Elasticsearch** — 청크 단위 검색 인덱스
3. **SQLite Graph DB** — 개념/관계 그래프
4. **MCP 서버** — Claude Code가 호출하는 기본 작업 인터페이스

MCP 흐름에서는 Claude Code 자체가 에이전트 역할을 합니다. CLI는 색인·검색·디버깅용 보조 경로입니다.

---

## 전체 다이어그램

```
[사용자]
  ↓
[Claude Code]
  ↓ MCP (stdio)
[PKB MCP 서버]  src/pkb/mcp_server.py
  ├─ search_knowledge        → Elasticsearch 검색
  ├─ write_file              → data/ 작성 + 자동 인제스트
  ├─ add_document            → data/ 파일 인제스트
  ├─ convert_and_ingest      → 외부 파일 → data/<category>/.md → 인제스트
  ├─ sync_obsidian           → Obsidian 볼트 일괄 인제스트
  ├─ get_document            → 문서 청크/section_path 조회
  ├─ reindex_document        → 단일 문서 재인제스트
  ├─ archive_document        → 소프트 삭제 (검색 제외, 복구 가능)
  ├─ restore_document        → 아카이브 복구
  ├─ doctor                  → 상태 점검
  └─ graph_*                 → SQLite 개념 그래프 조회/저장

[인제스트 파이프라인]  src/pkb/ingest.py
  원본 파일
    → markitdown 변환
    → YAML frontmatter 파싱 (title, tags, expires_at)
    → H1~H3 section_path 기반 계층 청킹
    → content_hash 계산 → 기존 청크와 델타 비교
    → 변경된 청크만 sentence-transformers 임베딩
    → Elasticsearch 델타 적용 (index / update / delete)

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
    → sync_concept_notes로 data/concepts/<slug>.md 노트 투영
    → 개념 관계 열람은 노트 파일을 직접 읽음
```

보조 인터페이스:

```
CLI       src/pkb/cli.py    pkb query, pkb reindex, pkb graph stats/sync-notes
```

---

## 1. 원본 저장소

### `data/`

`data/`는 개인 문서 저장소입니다. git에서 추적하지 않는 것을 기본으로 합니다.

실제 위치는 `DATA_ROOT`(기본: 프로젝트 내 `data/`)로 지정합니다. Obsidian 볼트 하위(예: `<vault>/PKB`)로 두면 원본을 Obsidian과 공유하며, doc_id는 위치와 무관하게 `data/<상대경로>`로 고정됩니다. Obsidian Vault 크롤은 `DATA_ROOT` 서브트리를 제외하고 진행합니다.

아래는 고정 스키마가 아니라 예시입니다. **최상위 폴더명이 곧 카테고리이며 동적입니다** — 코드가 카테고리 목록을 강제하지 않으므로, 폴더를 새로 만들면 그게 새 카테고리가 됩니다(`ingest.py:_extract_category`).

```
data/                       # = DATA_ROOT. doc_id는 위치 무관하게 항상 data/…
│                           # 최상위 폴더명 = 카테고리 (동적 — 목록 강제 없음)
├── career/                 # ┐ 관례상의 기본 카테고리 (예시일 뿐)
├── study/                  # │
│   ├── rag/ …              # │   하위 폴더 계층은 자유
│   └── reading/
│       ├── _review/        # │   _review·_trash: 이름 고정 · 색인 제외
│       └── _trash/         # │   (트리 어디에 있든 경로에 있으면 빠짐)
├── writing/                # ┘
├── projects/               # ← 임의로 추가한 카테고리 (폴더만 만들면 됨)
├── journal/                # ← 이름 자유
├── note.md                 # 루트 직속 파일 → 카테고리 misc (폴백 이름 고정)
├── .logs/                  # 검색 로그 JSONL — 경로 하드코딩 (search_log.py)
└── .graph/                 # 그래프 SQLite — GRAPH_DB_PATH로 설정 가능
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
    "language": "ko",
    "content_hash": "sha256(content)",   // 델타 임베딩 비교용
    "expires_at": null,                   // frontmatter로 설정 시 만료일 (검색 자동 제외)
    "archived_at": null                   // 아카이브 시각 (소프트 삭제)
}
```

### 청킹

- YAML frontmatter가 있으면 `title`, `tags` 등 메타데이터를 파싱
- H1~H3 헤딩 경로를 `section_path`로 저장
- 섹션 내부는 기본 500토큰, 100토큰 오버랩으로 분할
- 검색 결과에는 `section_path`가 함께 반환됨

### 델타 임베딩

문서를 다시 인제스트할 때 변경된 청크만 재임베딩한다. 각 청크에 `content_hash`(SHA-256)를
저장해 두고, `chunk_index` 단위로 기존 청크와 비교한다 (`ingest.py:ingest_files`).

| 비교 결과 | 동작 | 임베딩 비용 |
|-----------|------|------------|
| hash 동일 | 재사용 | 0 |
| hash 동일 + 메타데이터만 차이 | partial update | 0 |
| hash 불일치 / 신규 슬롯 | 재임베딩 + index | 변경분만 |
| 기존에만 있던 슬롯 | delete | 0 |

`store.apply_chunk_delta`가 index/update/delete를 한 번의 bulk로 적용한다. 단, 문서 앞쪽에
단락을 추가하면 이후 `chunk_index`가 전부 밀려 사실상 전체 재임베딩이 된다(끝부분 수정은 효율적).

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
| `graph_runs` | 그래프 빌드 실행 기록 |

그래프 구축 (전량 Claude Code 셀프추출, API 호출 없음):

1. Claude Code가 `graph_list_chunks(category="study", limit=20)`로 청크를 읽음
2. Claude Code가 청크 내용에서 개념과 관계를 추출
3. `graph_store_concepts(items_json=...)`로 SQLite에 저장
4. `sync_concept_notes()`로 `data/concepts/<slug>.md` 노트에 투영
5. 개념 관계 조회는 투영된 노트 파일을 직접 읽음

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
| 생명주기 | `archive_document`, `restore_document` |
| 상태 | `doctor` |
| Graph RAG | `graph_list_chunks`, `graph_store_concepts`, `sync_concept_notes` |

전송(transport)은 `stdio`로, 로컬 Claude Code가 `python -m pkb.mcp_server`를 직접 기동한다.

MCP 서버가 지키는 경계:

- `write_file`은 `data/` 하위 `.md`만 작성
- `add_document`는 `data/` 하위만 인제스트
- `convert_and_ingest`는 외부 파일을 읽을 수 있지만 결과는 `data/<category>/`에 저장
- Obsidian 동기화는 원본 볼트를 수정하지 않고 ES에만 반영

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
uv run pkb graph sync-notes
```

---

## 6. 문서 생명주기

### 상태 전이

```
신규/수정 ─ ingest_files (델타 임베딩)
아카이브 ─ archived_at = now   → 검색 제외, 복구 가능 (archive/restore_document)
만료    ─ expires_at < now    → 검색 자동 제외 (frontmatter로 설정)
삭제    ─ delete_document      → 하드 삭제 (비가역) / purge-archived
```

`retrieve.py`의 `_lifecycle_filter`가 기본 검색에서 `archived_at`이 있거나 `expires_at`이
지난 청크를 걸러낸다. `include_archived=True`면 필터를 끈다.

`data/` 하위 `_review/`·`_trash/` 디렉터리는 `find_ingestable_files`가 색인에서 제외한다
(`ingest.py:EXCLUDED_DIR_NAMES`). 검토 대기/폐기 노트를 코퍼스와 분리해 두는 용도다.

---

## 7. 운영

`docker-compose.yml`은 `pkb-es`(Elasticsearch) 컨테이너만 띄운다. MCP 서버와 CLI는
`uv run`으로 호스트에서 직접 실행하고, ES는 `localhost:9200`으로 접속한다.

```bash
docker compose up -d          # pkb-es (localhost:9200)
uv run pkb reindex --yes      # data/ + Obsidian 전체 재색인
```

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
  ├─ data/concepts/dependency-injection.md 등 관련 노트를 직접 읽음
  ├─ 노트의 [[위키링크]] 관계를 따라가며 연결 파악
  └─ 필요한 경우 get_document로 원문 근거 청크 확인
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
| CLI | 보조 인터페이스 | 재인덱싱, 디버깅, 검색 품질 확인에 적합 |

---

## 교체 가능 지점

- **Elasticsearch → Qdrant/Chroma/pgvector**: `store.py`, `retrieve.py` 교체
- **sentence-transformers → 다른 임베딩**: `embeddings.py` 교체
- **SQLite Graph DB → Neo4j**: `src/pkb/graph/store.py` 계층 교체
- **markitdown → docling/unstructured**: `ingest.py`의 파일 읽기 경로 교체
- **Claude Code MCP → 다른 MCP 클라이언트**: `mcp_server.py`는 그대로 사용 가능
