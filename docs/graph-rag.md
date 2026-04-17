# SQLite Graph RAG MVP

## 목적과 경계

**목적**: *"내 자료 전체의 개념이 어떻게 연결돼 있나"* 수준의 질문에 답한다. 기존 RRF+리랭커 검색이 잘 못하는 영역을 **보완**한다 — 대체가 아니다.

**현재 상태**: SQLite 기반 Graph RAG MVP는 구현됨. 기본 운영 경로는 **Claude Code + MCP**이고, `pkb graph build`는 운영/일괄 빌드용 보조 CLI다.

| | 기존 (ES + RRF + 리랭커) | 그래프 RAG |
|---|---|---|
| 잘하는 것 | "DI란?", "BM25 공식은?" 같은 **구체 질의** | "DI·IoC·Bean·Container가 어떻게 얽혀?", "학습 로드맵" 같은 **관계/전역 질의** |
| 데이터 단위 | 청크 (500토큰) | 개념(entity) + 관계(relation) |
| 응답 재료 | 본문 청크 | 그래프 구조 + 연결된 청크 |
| 빌드 시점 | 인제스트 즉시 | MCP `graph_list_chunks`/`graph_store_concepts` 또는 CLI `pkb graph build`로 명시 실행 |

**안 하는 것 (MVP 범위 밖)**:
- 자동 전체 그래프 빌드 (카테고리/폴더 단위 opt-in만)
- Neo4j/JanusGraph 같은 풀 그래프 DB
- GNN 기반 임베딩
- 대화 히스토리 기반 그래프 업데이트

---

## 저장 구조

**파일 위치**: `data/.graph/pkb_graph.sqlite` (gitignored)

### 스키마

```sql
-- 개념 노드 (중복 제거된 정규화 개념)
CREATE TABLE concepts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,              -- 정규화된 이름 (예: "Dependency Injection")
    slug            TEXT UNIQUE NOT NULL,       -- 소문자/공백 제거 (dedup 키)
    category        TEXT,                       -- 유래 카테고리 (obsidian/study/...)
    description     TEXT,                       -- LLM이 요약한 1~2문장
    embedding       BLOB,                       -- 384-dim float32 (dedup + 의미 검색용)
    mention_count   INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 별칭 (DI → Dependency Injection)
CREATE TABLE concept_aliases (
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    alias           TEXT NOT NULL,
    alias_slug      TEXT NOT NULL,
    PRIMARY KEY (concept_id, alias_slug)
);

-- 문서 노드 (ES doc_id와 1:1)
CREATE TABLE documents (
    id              INTEGER PRIMARY KEY,
    doc_id          TEXT UNIQUE NOT NULL,       -- ES doc_id와 동일 ("obsidian/Spring/...")
    title           TEXT,
    category        TEXT
);

-- 개념-개념 관계 (가중 그래프)
CREATE TABLE concept_edges (
    src_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    dst_id          INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,              -- "related_to" | "part_of" | "prerequisite_of" | "example_of" | 기타
    weight          REAL DEFAULT 1.0,           -- 동시 출현 빈도 등으로 가중
    evidence_count  INTEGER DEFAULT 1,          -- 관계 근거가 된 청크 수
    PRIMARY KEY (src_id, dst_id, relation)
);

-- 개념이 등장하는 청크 (그래프 → ES 청크로 이동하는 브리지)
CREATE TABLE concept_mentions (
    concept_id      INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    doc_id          TEXT NOT NULL,              -- ES doc_id
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,
    PRIMARY KEY (concept_id, doc_id, chunk_index)
);

-- 빌드 실행 기록 (재실행/증분 판단용)
CREATE TABLE graph_runs (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    scope_category  TEXT,
    scope_doc_id    TEXT,
    chunks_processed INTEGER,
    concepts_added  INTEGER,
    edges_added     INTEGER,
    model           TEXT,                       -- "claude-haiku-4-5-20251001"
    status          TEXT                        -- "success" | "failed" | "cancelled"
);

CREATE INDEX idx_concepts_slug ON concepts(slug);
CREATE INDEX idx_concepts_category ON concepts(category);
CREATE INDEX idx_concept_edges_src ON concept_edges(src_id);
CREATE INDEX idx_concept_edges_dst ON concept_edges(dst_id);
CREATE INDEX idx_concept_mentions_doc ON concept_mentions(doc_id);
CREATE INDEX idx_aliases_slug ON concept_aliases(alias_slug);
```

### 왜 SQLite?
- 단일 파일, 설치 불필요, 백업 쉬움
- 수천~수만 노드 규모에서는 충분히 빠름
- Python `sqlite3`만 쓰면 외부 라이브러리 최소화
- 나중에 정말 필요하면 `networkx`로 메모리 로드 후 그래프 알고리즘 적용

---

## 추출 파이프라인

### 1. 대상 선정

MCP 기본 흐름:

- `graph_list_chunks(category=..., limit=...)` → 해당 카테고리 청크 페이지 조회
- `graph_list_chunks(doc_id=..., limit=...)` → 단일 문서 청크 조회
- `category`와 `doc_id`가 모두 없으면 거부

CLI 보조 흐름:

- `pkb graph build --category study`
- `pkb graph build --doc-id obsidian/...`
- 둘 다 없으면 거부

### 2. 청크별 개념/관계 추출

MCP-first 방식에서는 Claude Code가 `graph_list_chunks` 결과를 직접 읽고 아래 규칙으로 개념/관계를 추출한다. 이 경우 프로젝트 `.env`의 `ANTHROPIC_API_KEY`가 필수는 아니다.

CLI `pkb graph build`를 쓰면 내부 빌더가 `GRAPH_EXTRACT_MODEL`로 설정된 Claude Haiku를 호출한다.

프롬프트 개요:

```
다음 텍스트에서 도메인 개념(entity)과 관계(relation)를 추출하세요.
결과는 JSON.

규칙:
- 개념은 명사구. 일반명사("방법", "예시")는 제외
- 관계는 concept1 → [관계타입] → concept2 형태
- 관계 타입: related_to, part_of, prerequisite_of, example_of (필요하면 다른 라벨도 허용)
- 각 개념에 1문장 설명 추가
- 최대 10개 개념, 15개 관계

텍스트:
{chunk_content}

출력 JSON 스키마:
{
  "concepts": [
    {"name": "Dependency Injection", "aliases": ["DI"], "description": "객체 간 의존성을 외부에서 주입하는 설계 원칙"}
  ],
  "relations": [
    {"src": "Dependency Injection", "dst": "IoC", "type": "part_of"}
  ]
}
```

**왜 Haiku**: 청크당 수백~수천 토큰, 구조화된 출력. Sonnet은 과투자. 품질 이슈 발견 시에만 Sonnet 승격.

### 3. 개념 정규화 (dedup)
1. **Slug 일치**: `dependency injection` == `Dependency Injection`
2. **Alias 일치**: "DI" → 기존 "Dependency Injection" concept에 매핑
3. **임베딩 유사도** (≥ 0.88): 기존 개념과 의미 매칭 → merge
4. 새 개념이면 insert, 기존이면 `mention_count += 1`, alias 추가

### 4. 관계 엣지 누적
- 같은 (src, dst, relation) 조합 재등장 → `weight += 1`, `evidence_count += 1`
- 각 언급 청크 → `concept_mentions` 에 기록

---

## MCP 도구 5개

### `graph_list_chunks`
```python
graph_list_chunks(
    category: str = "",
    doc_id: str = "",
    offset: int = 0,
    limit: int = 20,
) -> str
```
- ES 청크를 JSON으로 반환
- `limit` 최대 50
- Claude Code가 반환된 청크를 읽고 개념/관계를 추출

### `graph_store_concepts`
```python
graph_store_concepts(items_json: str) -> str
```
- Claude Code가 추출한 개념/관계 JSON을 SQLite에 저장
- 개념 alias, mention, edge를 함께 upsert
- 임베딩 기반 중복 병합 사용

### `search_concepts`
```python
search_concepts(query: str, top_k: int = 10) -> str
```
- 질의 임베딩 → concepts.embedding과 코사인 유사도 top-k
- 각 개념의 `mention_count`, `category`, 설명 1줄, 연결된 관계 수 반환

### `explain_concept`
```python
explain_concept(name: str, depth: int = 1) -> str
```
- 이름/slug/alias로 concept 조회
- `description` + 직접 관계(`depth=1`) + 등장 문서 목록
- `depth=2`면 이웃의 이웃까지 BFS
- 각 언급에는 ES doc_id + chunk_index → 필요하면 사용자가 `get_document`로 실제 내용 조회

### `related_concepts`
```python
related_concepts(
    name: str,
    relation: str = "",
    direction: str = "both",
    top_k: int = 20,
) -> str
```
- 특정 개념의 직접 이웃 (가중치 순)
- `relation` 필터: `"prerequisite_of"` 지정 시 해당 타입만
- `direction`: `out`, `in`, `both`

---

## 질의 패턴

### 1. 개념 관계 질의
> *"내 Spring 관련 노트에서 DI, IoC, Bean, Container가 어떻게 연결돼 있어?"*

Claude Code 흐름:
1. `search_concepts("DI IoC Bean Container")` → 4개 concept_id 확보
2. 각각 `explain_concept(..., depth=1)` 호출 → 관계 정보 수집
3. 4개 사이 공통 경로 찾기 (`related_concepts`로 교차)
4. 필요하면 각 핵심 언급 청크를 `get_document`로 읽어서 근거 제시

### 2. 전역 정리 질의
> *"RAG 자료 전체에서 검색 품질 개선 기법들을 관계 중심으로 정리해줘"*

1. `search_concepts("검색 품질 개선")` → 핵심 허브 concept 찾기
2. 해당 허브의 `related_concepts(depth=1~2)` → 그래프 부분 그래프 추출
3. Claude Code가 텍스트로 요약

### 3. 파이프라인 위치 질의
> *"HyDE, RRF, 리랭커, RAGAS는 각각 파이프라인 어디에 붙어?"*

1. 각 개념의 관계 중 `part_of` 엣지를 따라가면 자연스럽게 파이프라인 상위 개념에 도달
2. 상위 개념 기준으로 그룹화 답변

---

## 구현된 구성요소

### SQLite 스키마 + 저장 레이어
- `src/pkb/graph/schema.py`: SQLite 초기화
- `src/pkb/graph/store.py`: concept/document/alias/edge/mention CRUD, 임베딩 유사도 검색
- `src/pkb/graph/__init__.py`

### CLI 일괄 빌더
- `src/pkb/graph/extract.py`: Haiku 호출 + JSON 파싱
- `src/pkb/graph/prompts/extract.txt`: 개념/관계 추출 프롬프트
- `src/pkb/graph/builder.py`: ES 청크 순회 → 추출 → 정규화 → SQLite 저장
- `pkb graph build --category <cat>`
- `pkb graph stats`
- `pkb graph export <out.json|out.mmd>`

### MCP-first 그래프 도구
- `graph_list_chunks`: Claude Code가 읽을 청크 조회
- `graph_store_concepts`: Claude Code가 추출한 JSON 저장
- `search_concepts`: 개념 검색
- `explain_concept`: 개념 설명/관계/언급 문서 조회
- `related_concepts`: 직접 이웃 조회

MCP-first 방식이 기본이고, CLI 빌더는 많은 청크를 한 번에 처리해야 할 때 쓰는 보조 경로다.

---

## 비용 추정

- Haiku 청크당 평균 입력 ~1500 토큰, 출력 ~300 토큰
- 1M input ~$1, 1M output ~$5 (대략)
- study (830 청크): ~$2
- Obsidian (7930 청크): ~$15
- 전체 (8760 청크): ~$17

**권장**: 처음엔 MCP로 `study` 일부 청크부터 만들고, 유용성이 확인되면 `pkb graph build --category study` 또는 Obsidian 일부 문서로 확장한다.

---

## 열린 질문

1. **관계 타입을 고정 vs 자유?** MVP는 4개 고정 타입 + 자유 텍스트 허용. 쓰다 보면 규칙이 생길 것.
2. **ES 검색과의 통합?** MVP는 별도 도구로 분리. 나중에 `search_knowledge(use_graph=True)` 옵션 추가 고려 (질의를 개념 분해 후 각각 검색).
3. **임베딩을 같은 MiniLM?** 예. 개념 설명이 짧고 다국어이므로 새 모델 불필요.
4. **증분 빌드?** MVP에서는 `rebuild=True`로 scope 전체 재빌드만. 증분(수정된 청크만)은 나중에.
5. **Graph export 포맷?** JSON (프로그램 재사용) + Mermaid (노트에 삽입)

---

## 운영 체크리스트

- [x] `.env.example`에 `GRAPH_DB_PATH=data/.graph/pkb_graph.sqlite` 기본값 문서화
- [x] 추가 런타임 DB 의존성 없음 (`sqlite3` 표준 라이브러리 사용)
- [x] `src/pkb/graph/` 구현
- [x] MCP 그래프 도구 구현
- [x] CLI 그래프 빌드/통계/내보내기 구현
- [ ] 실제 사용 시에는 소규모 scope부터 구축 (`category=study` 일부 또는 단일 `doc_id`)
