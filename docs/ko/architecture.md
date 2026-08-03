# PKB 아키텍처

PKB는 **두 가지 일**을 합니다.

1. **Claude Code가 MCP로 개인 지식을 검색·작성**한다 (Elasticsearch 하이브리드 검색).
2. **사람이 Obsidian에서 마크다운을 읽는다** — 코퍼스 원본(`data/`)과, SQLite 개념 그래프에서
   투영된 개념노트(`data/_concepts/`) 둘 다 일반 마크다운 파일이라 그대로 열람됩니다.

100% 로컬입니다 — 외부 API 호출이 없고 `ANTHROPIC_API_KEY`도 필요 없습니다. 개념 추출·문서 작성
모두 Claude Code 세션 자체가 수행합니다. 코드에 존재하는 유일한 LLM 호출은 선택 기능이며 머신
안에 머뭅니다: `pkb graph rebuild-evidence-local`이 근거 재구축 루프를 자동화하려고 로컬 Ollama
엔드포인트(`http://127.0.0.1:11434`, `graph/rebuild.py`)로 요청을 보냅니다. 그 외에는 코드 어디서도
생성 모델과 통신하지 않습니다.

핵심 구성은 네 층입니다:

1. **`data/`** — 개인 문서 원본 (Source of Truth). 위치는 `DATA_ROOT`로 지정하며, Obsidian 볼트
   하위(예: `<vault>/PKB`)로 두는 것을 권장합니다.
2. **Elasticsearch** — 청크 단위 검색 인덱스 (nori BM25 + dense_vector kNN)
3. **SQLite Graph DB** — 개념/관계 그래프. `data/_concepts/`로 단방향 투영
4. **MCP 서버** — Claude Code가 호출하는 기본 인터페이스. CLI는 운영·검증용 보조 경로

---

## 전체 다이어그램

```
[사용자]
  ↓
[Claude Code]
  ↓ MCP (Streamable HTTP, 127.0.0.1:8787)
[PKB MCP 서버]  src/pkb/mcp_server.py  (도구 22개)
  ├─ search_knowledge        → ES 하이브리드 검색
  ├─ write_file / add_document / convert_and_ingest  → data/ 작성·인제스트
  ├─ list_documents / get_document / reindex_document
  ├─ sync_corpus / sync_obsidian     → 원본↔ES 재조정(유령 문서 정리)
  ├─ archive_document / restore_document → 소프트 삭제/복구
  ├─ doctor                  → 상태 점검
  └─ graph_*(어휘·청크·저장·큐레이션·병합) / sync_concept_notes → 개념 그래프

[사람]
  ↓ Obsidian로 직접 열람
  data/ 원본 + data/_concepts/*.md 투영 노트
```

### 데이터 흐름

```
[인제스트]  src/pkb/ingest.py
  data/의 문서(md/pdf/docx/…)
    → frontmatter 파싱 → 헤딩 계층 청킹(+ 초소형 청크 병합)
    → content_hash 델타 비교 → 변경분만 임베딩
    → Elasticsearch bulk 적용(index/update/delete)

[그래프]  src/pkb/graph/
  ES 청크 → graph_list_chunks → Claude Code 셀프추출 → graph_store_concepts
    → data/.graph/pkb_graph.sqlite 저장 → sync_concept_notes → data/_concepts/<slug>.md 투영
```

보조 인터페이스: `src/pkb/cli.py` (`pkb init/reindex/sync/query/graph …`). CLI와 MCP 도구는
`tests/test_cli_mcp_parity.py`가 introspection으로 정합을 강제합니다 — 새 명령/도구를 추가하면
이 테스트가 매핑 누락을 잡아냅니다.

---

## 1. 원본 저장소 (`data/`)

실제 위치는 `DATA_ROOT`(기본: 프로젝트 내 `data/`)로 지정합니다. **Obsidian 볼트 하위 폴더로
지정하는 것을 권장**합니다(예: `<vault>/PKB`) — 코퍼스 자체가 볼트 안에 있어 Obsidian으로 바로
열람·편집·백링크할 수 있고, `write_file`/`sync_concept_notes` 산출물도 볼트에 그대로 나타납니다.
doc_id는 위치와 무관하게 항상 `data/<상대경로>`로 고정됩니다.

**최상위 폴더명이 곧 카테고리이며 동적입니다** — 코드가 카테고리 목록을 강제하지 않으므로,
폴더를 새로 만들면 그게 새 카테고리가 됩니다(`ingest.py:_extract_category`).

```
data/                       # = DATA_ROOT. doc_id는 위치 무관하게 항상 data/…
├── study/                  # 폴더=카테고리. 하위 계층도 자유 (예: rag/retrieval/, rag/rerank/…)
├── career/
├── writing/
├── about/
├── ...                     # 폴더 추가 = 카테고리 추가 (news, journal, agent, backend…)
├── _concepts/              # 개념노트 (SQLite→노트 단방향 투영, ES 미색인)
├── .logs/                  # 검색 로그 JSONL (search_log.py)
└── .graph/                 # 그래프 SQLite (GRAPH_DB_PATH)
```

위 폴더명은 예시일 뿐입니다 — 코드는 고정 목록을 강제하지 않습니다. 코퍼스를 어떻게 나눌지는
정책 선택입니다: 전부 `data/` 아래에 두거나, 주제 자료만 `data/`에 두고 개인 폴더는 볼트의 다른
곳에 남겨 아래 설명하는 선택적 `obsidian/` 크롤로 잡히게 할 수 있습니다. 청크/문서 수는 폴더
구성에 따라 달라지므로 최신 수치는 `list_documents`로 확인합니다.

색인 제외 대상(`ingest.py:EXCLUDED_DIR_NAMES`, `is_concept_path`):

| 대상 | 이유 |
|---|---|
| `_review/`, `_trash/`, `_materials/`, `_archive/` | 검토 대기·폐기·중복·보관 원본 |
| `_origin/` | 외부 원본 보관소 — 소화 노트만 색인, 원본은 근거 확인용 |
| `.`으로 시작하는 폴더 전체 | 도구 산출물 (`.obsidian` 등) |
| `data/_concepts/` | 개념노트는 SQLite→노트 투영본이라 ES 중복 색인 금지 |

지원 포맷:

| 확장자 | 처리 |
|-------|------|
| `.md`, `.markdown`, `.txt` | 직접 읽기 |
| `.pdf` | `pdfminer`로 페이지 마커(`## p.N`) 보존 추출 |
| `.docx`, `.pptx`, `.xlsx`, `.html` | `markitdown`으로 마크다운 변환 |

### Obsidian (`OBSIDIAN_PATH`) — 코퍼스 밖에 파일을 둔 경우의 선택 경로

기본 그림은 "전부 `data/` 코퍼스"입니다. `data/`가 이미 볼트 안에 있으므로 보통은 이 메커니즘이
불필요합니다. 다만 볼트에 `DATA_ROOT` 밖 파일(개인 폴더 about·career·writing·운동 등, PKB로
옮기지 않은 일반 노트 등)을 남겨두고 같이 검색하고 싶다면 `.env`의 `OBSIDIAN_PATH`를 설정합니다
— **카테고리는 볼트 첫 폴더명에서 동적으로 파생**됩니다(`data/`와 동일 규칙,
`ingest.py:_extract_category`), `doc_id=obsidian/<볼트 상대경로>`로 별도 크롤합니다(예:
`obsidian/career/x.md` → `category=career`). `DATA_ROOT` 서브트리는 자동 제외되어 이중
인제스트는 없습니다. 원본 파일은 복사·수정하지 않습니다.

---

## 2. Elasticsearch 검색 인덱스

Docker 컨테이너 `pkb-es`로 실행되며, 기본 인덱스는 `pkb_documents`입니다.

### 저장 단위

파일 하나는 여러 청크로 분할되고, 각 청크가 ES 문서 1개(`_id = f"{doc_id}_{chunk_index}"`,
예: `data/study/rag/example.md_0`)로 저장됩니다(`store.add_chunks`/`apply_chunk_delta`).

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
    # 아래 lifecycle 필드는 설정된 경우에만 존재 — 기본은 null이 아니라 "필드 자체가 없음"
    # "expires_at": "...",     frontmatter에 설정했을 때만
    # "archived_at": "...",    archive_document 호출 시에만
    # "archive_reason": "...", 아카이브 사유를 준 경우에만
}
```

### 청킹

- YAML frontmatter가 있으면 `title`, `tags`, `expires_at` 등 메타데이터를 파싱
- H1~H3 헤딩 경로를 `section_path`로 저장 (없으면 파일 경로에서 파생)
- 섹션 내부는 기본 500토큰, 100토큰 오버랩으로 분할 (`CHUNK_SIZE`/`CHUNK_OVERLAP`)
- 80자 미만(`MIN_CHUNK_CHARS`) 초소형 청크는 인접 청크에 병합 — 헤딩·링크만 있는 저정보
  청크가 리랭크 상위를 점령하는 것을 방지 (`ingest.py:_merge_tiny_chunks`)

### 델타 임베딩

문서를 다시 인제스트할 때 변경된 청크만 재임베딩합니다. 각 청크의 `content_hash`(SHA-256)를
`chunk_index` 단위로 기존 청크와 비교합니다(`ingest.py:ingest_files`).

| 비교 결과 | 동작 | 임베딩 비용 |
|-----------|------|------------|
| hash 동일 | 재사용 | 0 |
| hash 동일 + 메타데이터만 차이 | partial update | 0 |
| 슬롯 불일치, 같은 hash가 문서 내 다른 슬롯에 존재 | 임베딩 복사(moved) | 0 |
| 그 외 신규/변경 | 재임베딩 + index | 변경분만 |
| 새 청크에서 사라진 슬롯 | delete | 0 |

`store.apply_chunk_delta`가 index/update/delete를 한 번의 bulk로 적용합니다.

### 검색 파이프라인

`search_knowledge`(MCP)와 `pkb query`(CLI)는 같은 파이프라인(`retrieve.hybrid_search`)을
공유합니다.

1. BM25 검색 — nori 한국어 분석기, `content`/`title`/`section_path` 가중 매치
2. kNN 검색 — sentence-transformers 임베딩, ES dense_vector(HNSW)
3. RRF 결합 — 두 후보 집합을 Reciprocal Rank Fusion으로 결합(`RRF_K=60`, 고정 로직)
4. CrossEncoder 재순위 — 기본 비활성(`RERANK_ENABLED=false`), 모델 `BAAI/bge-reranker-v2-m3`.
   2026-07 벤치에서 BGE-M3 후보 풀 기준 무재순위(MRR 0.517)가 bge(0.388)·Qwen3-0.6B(0.492)를 앞서 기본 off.
5. **문서당 최대 2청크 캡**(`MAX_CHUNKS_PER_DOC`) — 한 문서가 상위권을 독점하지 않도록 다양성 확보
6. 선택: `EXPAND_CONTEXT=N` — 결과마다 전후 N청크를 `neighbors`로 부착

생명주기 필터(`retrieve._lifecycle_filter`)가 기본적으로 `archived_at` 존재 청크와
`expires_at`이 지난 청크를 제외합니다. `search_knowledge` 파라미터: `query`, `category`,
`top_k`, `include_archived`, `include_obsidian`(기본 True — False면 `obsidian/` 접두 doc_id,
즉 코퍼스 밖 볼트 직속 문서를 제외).

검색 호출은 `data/.logs/search.jsonl`에 JSONL로 기록됩니다.

---

## 3. SQLite 개념 그래프

Graph RAG는 검색을 대체하지 않고 개념 간 관계 질의를 보완합니다. 저장 위치는
`data/.graph/pkb_graph.sqlite`(`GRAPH_DB_PATH`)이며, 주요 테이블은 `concepts`,
`concept_aliases`, `documents`, `concept_edges`, `concept_edge_evidence`, `extracted_chunks`,
`concept_mentions`, `concept_curation`, `graph_meta` 9개가 전체 테이블입니다(`graph/schema.py`).
`concept_edges`의 weight/evidence_count는 청크별
`concept_edge_evidence`에서 집계되어 재추출·문서 삭제에도 정확히 정리됩니다.

빌드 파이프라인(전량 Claude Code 셀프추출, API 호출 없음):

1. `graph_list_chunks(category|doc_id, offset, limit)`로 청크를 페이지 단위로 읽음
2. Claude Code가 청크 내용에서 개념/관계를 추출
3. `graph_store_concepts(items_json)`로 SQLite에 저장 (정규화·alias·mention·edge 반영)
4. `sync_concept_notes(confirm_prune)`로 `data/_concepts/<slug>.md` 노트에 단방향 투영 (+ `_concepts/index.md` MOC 렌더)
5. `graph_explain`, `graph_path`, `graph_query`, `graph_affected`로 SQLite를 직접 조회 — 반환 엣지마다
   confidence와 제한된 출처 청크 evidence 포함
6. 투영 노트는 사람이 읽는 Obsidian 뷰로 사용하고, 원문 내용은 `search_knowledge`로 조회

상세 설계·정규화 규칙·노트 렌더링 형식은 [docs/graph-rag.md](graph-rag.md) 참조.

---

## 4. MCP 서버

`src/pkb/mcp_server.py`가 PKB의 기본 인터페이스입니다. 전송은 Streamable HTTP이며,
launchd로 상시 기동한 단일 서버를 Claude Code·Codex·Gemini가 공유합니다.

```
Claude Code / Codex / Gemini → HTTP :8787/mcp → mcp_server.py ─┐
                                                               ├→ operations.py / documents.py
uv run pkb <command> ────────────────────────→ cli.py ─────────┘   (공유 도메인 코어)
                                                                          ↓
                                                                 ES / data / SQLite
```

`mcp_server.py`와 `cli.py`는 둘 다 얇은 표면입니다. 작성·변환·동기화 도메인 작업은
`src/pkb/operations.py`에, 문서 경로 해석·조회·생명주기는 `src/pkb/documents.py`에 있습니다 —
도구와 그 CLI 쌍둥이가 서로 어긋나지 못하도록 추출한 계층입니다. 위에서 말한 CLI↔MCP 정합을
실제로 성립시키는 것이 이 공유 코어입니다. `tests/test_cli_mcp_parity.py`는 introspection으로
*표면*을 가드해 — 새로 등록된 명령·도구가 capability 맵이나 allowlist에 선언되지 않으면 실패 —
동작 자체를 비교하지는 않습니다. 동작 일치는 공유 코어가 담당합니다.

제공 도구 22개:

| 범주 | 도구 |
|------|------|
| 검색 | `search_knowledge` |
| 파일/문서 | `write_file`, `list_documents`, `add_document`, `convert_and_ingest`, `get_document`, `reindex_document`, `sync_corpus`, `sync_obsidian` |
| 생명주기 | `archive_document`, `restore_document` |
| 상태 | `doctor` |
| Graph RAG | `graph_explain`, `graph_path`, `graph_query`, `graph_affected`, `graph_list_concepts`, `graph_list_chunks`, `graph_store_concepts`, `graph_curate`, `graph_merge`, `sync_concept_notes` |

각 도구의 파라미터와 사용 예시는 [docs/mcp.md](mcp.md) 참조.

MCP 서버가 지키는 경계:

- `write_file`은 `data/` 하위 `.md`만 작성
- `add_document`는 `data/` 하위만 인제스트
- `convert_and_ingest`는 외부 파일을 읽을 수 있지만 결과는 항상 `data/<category>/`에 저장
- Obsidian 동기화(`sync_obsidian`)는 원본 볼트를 수정하지 않고 ES에만 반영

---

## 5. 보조 인터페이스 (CLI)

`src/pkb/cli.py`는 운영과 검증에 사용합니다. 최상위 명령 19개: `init`, `reindex`, `index-switch`,
`sync`, `convert`, `add`, `write`, `show`, `reindex-doc`, `list`, `query`, `delete`, `archive`,
`restore`, `doctor`, `eval`, `purge-archived`, `stale`, `watch`. 여기에 `graph` 하위 명령 10개:
`stats`, `explain`, `path`, `query`, `affected`, `map`, `reset-evidence`, `finalize-evidence`,
`rebuild-evidence-local`, `sync-notes`.

대부분의 능력은 CLI와 MCP 양쪽에 있습니다(`sync` ↔ `sync_corpus`, `show` ↔ `get_document`,
`graph explain` ↔ `graph_explain`, `graph map` ↔ `graph_map` 등). CLI 전용: `init`, `reindex`,
`index-switch`(읽기 alias 전환), `delete`, `purge-archived`, `eval`, `graph stats`,
`graph reset-evidence`, `graph rebuild-evidence-local`, `graph finalize-evidence`
(무거운 evidence 마이그레이션), `stale`,
`watch`(훅·데몬용). MCP 전용: `graph_list_concepts`,
`graph_list_chunks`, `graph_store_concepts`, `graph_curate`, `graph_merge`(Claude 셀프추출
루프), `sync_obsidian`(볼트 전용 재조정). 이 매핑은 `tests/test_cli_mcp_parity.py`가 가드합니다.

### 도구 프로파일 (`PKB_MCP_PROFILE`)

MCP 서버 프로세스에 `PKB_MCP_PROFILE=core`를 주면 전체 23개 대신 `CORE_TOOLS`
(`mcp_server.py`)의 9개만 노출합니다 — 실제로 호출되던 것들에 `graph_map`을 더한 구성으로,
그래프를 보려고 CLI로 나갈 일을 없앴습니다. 기본값은 `full`입니다.
서버 `instructions`도 프로파일에 따라 바뀌어 없는 도구를 안내하지 않으며,
`tests/test_cli_mcp_parity.py`가 이를 가드합니다.

---

## 6. 문서 생명주기

```
신규/수정 ─ ingest_files (델타 임베딩)
아카이브 ─ archived_at = now   → 검색 제외, 복구 가능 (archive/restore_document)
만료    ─ expires_at < now    → 검색 자동 제외 (frontmatter로 설정)
삭제    ─ delete_document      → 하드 삭제 (비가역) / purge-archived
```

`retrieve.py`의 `_lifecycle_filter`가 기본 검색에서 `archived_at`이 있거나 `expires_at`이
지난 청크를 걸러냅니다. `include_archived=True`면 필터를 끕니다.

---

## 7. sync 책임 매트릭스

| 상황 | 실행 |
|---|---|
| 파일 하나를 직접 수정(에디터/Obsidian에서 편집) | `pkb reindex-doc <doc_id>` / MCP `reindex_document` |
| 파일 다수 추가·이동·삭제(대량 변경) | `pkb sync` / MCP `sync_corpus`(+ 필요 시 `sync_obsidian`) |
| 개념 그래프를 최신 내용으로 갱신 | Claude 셀프추출(`graph_list_chunks`→`graph_store_concepts`) 후 `pkb graph sync-notes` / MCP `sync_concept_notes` |
| 매핑 스키마 변경·인덱스 상태가 꼬임 | `pkb reindex --yes` (전체 삭제 후 재구축) |

---

## 8. 운영

`docker-compose.yml`은 `pkb-es`(Elasticsearch) 컨테이너만 띄웁니다. MCP 서버와 CLI는
`uv run`으로 호스트에서 직접 실행하고, ES는 `localhost:9200`으로 접속합니다.

```bash
docker compose up -d          # pkb-es (localhost:9200)
uv run pkb sync                # 일상 재조정 (델타 업서트 + 유령 문서 정리)
uv run pkb reindex --yes      # data/ + Obsidian 전체 재색인 (매핑 변경 시)
```

---

## 실제 흐름 예시

### 예시 1: 자료 기반 정리 노트 생성

```
사용자: "저장된 BM25 관련 내용 정리해서 data/writing/bm25.md에 저장해줘"
  ↓
Claude Code
  ├─ search_knowledge(query="BM25", category="rag")
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
       ├─ pdfminer 페이지 보존 변환 (`## p.N` 마커 + provenance frontmatter)
       ├─ data/study/paper.md 저장
       └─ ES 인제스트
```

### 예시 3: 개념 관계 질의

```
사용자: "DI, IoC, Bean, Container가 어떻게 연결돼 있어?"
  ↓
Claude Code
  ├─ graph_query(query="DI, IoC, Bean, Container의 관계")
  ├─ 반환 엣지의 confidence와 doc_id/chunk_index evidence 확인
  └─ 필요한 경우 search_knowledge/get_document로 원문 근거 확인
```

---

## 왜 이 구조인가

| 구성요소 | 역할 | 선택 이유 |
|---------|------|-----------|
| MCP | 기본 인터페이스 | Claude Code가 바로 개인 지식 도구를 호출할 수 있음 |
| `data/` (볼트 내) | 원본 저장소 | 사람이 Obsidian으로 읽고 편집 가능한 단일 원본 |
| Elasticsearch | 검색 인덱스 | 한국어 키워드 검색과 벡터 검색을 함께 운용 |
| RRF + 리랭커 | 검색 품질 | 키워드/의미 검색의 장점을 결합하고 최종 정밀도 보정 |
| SQLite Graph DB | 개념 관계 저장 | 개인 규모에서 설치/운영 부담이 작고 백업 쉬움 |
| CLI | 보조 인터페이스 | 재인덱싱, 디버깅, 검색 품질 확인에 적합 |

## 교체 가능 지점

- **Elasticsearch → Qdrant/Chroma/pgvector**: `store.py`, `retrieve.py` 교체
- **sentence-transformers → 다른 임베딩**: `embeddings.py` 교체
- **SQLite Graph DB → Neo4j**: `src/pkb/graph/store.py` 계층 교체
- **markitdown → docling/unstructured**: `ingest.py`의 파일 읽기 경로 교체
- **Claude Code MCP → 다른 MCP 클라이언트**: `mcp_server.py`는 그대로 사용 가능
