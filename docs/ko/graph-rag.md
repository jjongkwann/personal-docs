# SQLite Graph RAG

## 목적과 경계

**목적**: *"내 자료 전체의 개념이 어떻게 연결돼 있나"* 수준의 질문에 답한다. 기존 RRF+리랭커 검색이 잘 못하는 영역을 **보완**한다 — 대체가 아니다.

**핵심 설계**: 개념 레이어는 100% Claude Code 셀프추출이다 — API 호출도 LLM 비용도 없다. Claude Code가 `graph_list_chunks`로 청크를 읽고, 스스로 개념/관계를 추출해 `graph_store_concepts`로 SQLite에 저장한다. `graph_explain`, `graph_path`, `graph_query`, `graph_affected`는 SQLite 그래프를 직접 조회하고, `sync_concept_notes`는 이를 `data/_concepts/<slug>.md` 볼트 노트로 투영해 사람이 열람하게 한다.

| | 기존 (ES + RRF + 리랭커) | 그래프 RAG |
|---|---|---|
| 잘하는 것 | "DI란?", "BM25 공식은?" 같은 **구체 질의** | "DI·IoC·Bean·Container가 어떻게 얽혀?" 같은 **관계/전역 질의** |
| 데이터 단위 | 청크 (500토큰) | 개념(entity) + 관계(relation) |
| 응답 재료 | 본문 청크 | `data/_concepts/` 노트 (산문 + 관계 링크 + 출처) |
| 빌드 시점 | 인제스트 즉시 | MCP `graph_list_chunks`/`graph_store_concepts`로 명시 실행 |

**안 하는 것**: 자동 전체 그래프 빌드(opt-in만), Neo4j 등 풀 그래프 DB, GNN 임베딩, 대화 히스토리 기반 자동 업데이트.

---

## 저장 구조

**파일 위치**: `data/.graph/pkb_graph.sqlite` (gitignored)

주요 테이블:

| 테이블 | 역할 |
|--------|------|
| `concepts` | 정규화된 개념 노드 (name, slug, description, embedding, mention_count) |
| `concept_aliases` | DI → Dependency Injection 같은 별칭 |
| `documents` | ES `doc_id`와 연결되는 문서 노드 |
| `concept_edges` | 개념 간 관계의 집계 투영 (relation, weight, evidence_count) |
| `concept_edge_evidence` | 관계를 추출한 `doc_id`/`chunk_index`별 근거 |
| `concept_mentions` | 개념이 등장한 `doc_id`/`chunk_index` |
| `concept_curation` | 개념 큐레이션(real/vocab) + 증류 산문 |
| `extracted_chunks` | 추출 완료 마커 `(doc_id, chunk_index)` → `content_hash` |
| `graph_meta` | key/value 마커 — 1회성 스키마 마이그레이션과 `edge_evidence_rebuild` staging 플래그 |

증분 추출은 `extracted_chunks`가 기준이다. 청크의 현재 `content_hash`가 그 인덱스의 마커와
다르면(= 내용 변경 또는 다른 인덱스로 이동) pending이 되어 재추출되고, 재추출은 **그 청크의
멘션과 관계 evidence를 교체**한다 — 개정된 청크에서 사라진 개념·관계는 남지 않는다. `chunk_index`가 NULL인
행은 해시로만 기록된 구마커로, 해당 청크 내용이 바뀔 때까지만 fallback으로 인정된다.
엣지 `weight`/`evidence_count`는 `concept_edge_evidence`의 실측 행 수에서 계산하므로 같은 청크를
재호출해도 부풀지 않는다. 문서 삭제·청크 이동·내용 변경 시 해당 evidence도 함께 정리된다.

스키마 정의는 `src/pkb/graph/schema.py`, CRUD는 `src/pkb/graph/store.py`.

### 개념 정규화 (dedup)
1. **Slug 일치**: `dependency injection` == `Dependency Injection`
2. **Alias 일치**: "DI" → 기존 "Dependency Injection" concept에 매핑
3. **임베딩 유사도** (≥ `GRAPH_DEDUP_THRESHOLD`, 기본 0.88): 기존 개념과 의미 매칭 → merge
4. 새 개념이면 insert, 기존이면 alias 추가 (`mention_count`는 `concept_mentions` 실측치로
   재계산 — 같은 청크를 재추출해도 부풀지 않는다)

dedup을 통과해 이미 별도 노드로 쪼개진 중복(표기 변형 등)은 `store.merge_concepts(conn,
winner_slug, loser_slugs)`로 사후 병합한다 — 엣지·mention·별칭·산문을 승자로 승계하고
loser 노트는 다음 `sync_concept_notes`에서 orphan으로 정리된다. 주의: "MCP Server"처럼
상위 개념의 **구성요소**는 표기 변형이 아니므로 병합 금지.

### 노트 투영 기준
`sync_concept_notes`는 전 개념이 아니라 **`concept_curation.label='real'`(실개념 분류)
이면서 관계(엣지)를 1개 이상 가진 개념**만 노트로 투영한다 — 고아(관계 0) 개념은 연결
가치가 없어 제외하되 SQLite에는 남는다(`store.projected_slugs`). 큐레이션 테이블이
비어 있으면 전량 투영으로 폴백. 미투영 개념을 가리키는 관계·산문 링크는 평문으로
렌더되어 깨진 위키링크가 생기지 않는다.

### 관계 엣지 집계
- 같은 청크의 (src, dst, relation) 재호출 → evidence 멱등 upsert (count 불변)
- 서로 다른 청크의 같은 관계 → `weight`/`evidence_count`를 evidence 수로 집계
- 각 언급 청크 → `concept_mentions`에 기록

---

## 파이프라인: 셀프추출 → 저장 → 노트 투영

### 1. `graph_list_chunks(category|doc_id, offset, limit, pending_only)`
ES 청크를 페이지 단위 JSON으로 반환. `pending_only=True`면 미추출·내용 변경 청크만 반환한다(증분 추출). Claude Code가 이 결과를 직접 읽고 아래 규칙으로 개념/관계를 추출한다. 추출 전 `graph_list_concepts`로 기존 어휘를 확인해 겹치는 개념은 기존 name/slug를 재사용한다.

- 개념: 구체적 명사구 (예: "Dependency Injection", "BM25"). 일반 단어/인명/지명 제외
- 관계 타입: `related_to` | `part_of` | `prerequisite_of` | `example_of` (필요 시 자유 라벨 허용)
- 청크당 개념 8개·관계 12개 이내

### 2. `graph_store_concepts(items_json)`
추출한 개념/관계 JSON을 SQLite에 upsert (정규화·alias·mention·edge 포함).

### 3. `sync_concept_notes(confirm_prune)`
SQLite → `data/_concepts/<slug>.md` 노트로 단방향 투영 (결정적·멱등). 개념 엣지를 `[[위키링크]]`로 되써서 Obsidian 그래프뷰가 개념그래프를 그리게 한다. 링크 타깃은 doc_id가 아니라 **볼트 물리 경로**(예: `[[PKB/_concepts/slug|이름]]`) — `DATA_ROOT`가 볼트 밖이면 파일명 링크로 폴백한다. 미투영(vocab) 개념으로의 관계는 평문으로 표시된다. `<!-- pkb:auto:start/end -->` 마커 사이만 재생성하므로 마커 밖 사용자 산문은 보존된다. 노트→SQLite 역승격은 없음(SQLite가 항상 SSOT). 투영 대상 개념을 category별로 묶은 `_concepts/index.md` MOC도 함께 렌더한다 — 개념 어휘 카탈로그 진입점. 구현: `src/pkb/graph/notes.py`.

### 4. 조회와 열람
단일 개념은 `graph_explain`, 최단 연결은 `graph_path`, 자연어 관계 질문은 의미 시드 기반
`graph_query`, 저장 방향 기준 하위 범위는 `graph_affected`로 조회한다. 모든 엣지에는 confidence와
제한된 `doc_id`/`chunk_index` evidence가 포함된다. `data/_concepts/<slug>.md` 노트는 사람이 읽는
Obsidian 뷰로 유지한다. 원문 내용이 필요한 질문은 `search_knowledge`를 사용한다.

---

## CLI 보조 명령

```bash
uv run pkb graph stats        # 개념/엣지/evidence/멘션/문서/별칭 통계
uv run pkb graph explain "BM25"  # 단일 개념과 양방향 관계 근거
uv run pkb graph path "BM25" "RRF"  # 제한된 최단 경로
uv run pkb graph query "키워드 검색과 벡터 검색은 어떻게 연결돼?"  # 의미 시드 하위 그래프
uv run pkb graph affected "BM25" --relation prerequisite_of  # 저장 방향 하위 순회
uv run pkb graph map --concept "BM25"   # 오프라인 Evidence Map HTML 스냅샷
uv run pkb graph reset-evidence --yes  # 기존 그래프 유지, staging evidence·마커 초기화
uv run pkb graph rebuild-evidence-local --yes  # Ollama 로컬 모델로 pending 전량 추출
uv run pkb graph finalize-evidence --yes  # 전량 추출 확인 후 staging 그래프로 원자 전환
uv run pkb graph sync-notes   # SQLite → data/_concepts/ 노트 동기화
uv run pkb graph sync-notes --yes   # 대량 정리(21개 이상) 확인 생략
```

구버전 append-only 엣지를 evidence 기반으로 전환할 때는 먼저 `reset-evidence --yes`를 한 번
실행하고, `graph_list_chunks(..., pending_only=True)` → `graph_store_concepts` 루프를 전 청크에
대해 다시 수행한다. 재구축 중에는 기존 관계·멘션을 계속 서비스하고 새 evidence만 staging에
쌓는다. pending이 0이 된 뒤 `finalize-evidence --yes`를 실행하면 기존 관계를 evidence 집계로
한 번에 교체한다. 개념·별칭·큐레이션 산문은 전 과정에서 보존된다.

Ollama에 생성 모델이 설치돼 있으면 `rebuild-evidence-local`이 structured output으로 이 루프를
자동화한다. 기본 모델은 `gpt-oss:20b`, 기본 배치는 8청크이며 중단해도 마커 기준으로 재개한다.
진행 로그는 `data/.logs/graph-evidence-rebuild.jsonl`에 기록된다. 샘플 검증에는
`--max-batches 1`, 전체 실행에는 기본값 `--max-batches 0`을 사용한다.

### Evidence Map (`graph map`)

`graph map`은 **단일 자급식 HTML 파일**을 생성한다 — 하위 그래프를 방사형 트리로 그리고 근거
패널과 관계 필터를 붙인 스냅샷으로, 서버도 네트워크도 필요 없다. 진입 모드는 정확히 하나만
지정한다:

```bash
uv run pkb graph map --concept "BM25"                    # 개념 하나를 중심으로
uv run pkb graph map --query "BM25와 RRF는 어떻게 연결돼?"   # 의미 시드 탐색
uv run pkb graph map --path BM25 RAG                     # 두 개념 사이 최단 경로
```

| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--depth` | `1` | 확장 깊이 (0~2) |
| `--max-nodes` | `30` | 표시 노드 상한 (1~100) |
| `--relation` / `-r` | 전체 | 관계 타입 필터(쉼표 구분) |
| `--evidence-limit` | `5` | 관계당 근거 수 (0~20) |
| `--out` | `<GRAPH_DB_PATH 디렉터리>/evidence-map.html` | 출력 경로 |
| `--open` | 끔 | 생성 후 브라우저로 열기 |

`--concept`/`--query`/`--path`를 하나도 안 주거나 둘 이상 주면 사용법 오류다. `--query`는 텍스트를
임베딩하므로 임베딩 모델을 로드하고, `--concept`·`--path`는 SQLite만 읽는다. 구현은
`src/pkb/graph/viewmap.py`.

일괄 API 빌드나 export CLI는 없다. 구축은 MCP-first로 유지하고, 조회는 SQLite 네이티브 그래프 도구,
CLI, Obsidian 투영 노트를 함께 제공한다.

---

## 왜 SQLite인가

- 단일 파일, 설치 불필요, 백업 쉬움
- 수천~수만 노드 규모에서는 충분히 빠름
- Python `sqlite3`만 쓰면 외부 라이브러리 최소화

## 왜 API 추출을 없앴는가

Claude Code가 이미 청크를 읽을 수 있는 세션이므로, 별도 LLM API 호출(과 비용·키 관리)은 중복이었다. `graph_list_chunks`/`graph_store_concepts` 두 도구만으로 Claude Code 세션 자체가 추출기로 동작한다.
