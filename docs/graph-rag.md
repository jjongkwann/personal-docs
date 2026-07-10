# SQLite Graph RAG

## 목적과 경계

**목적**: *"내 자료 전체의 개념이 어떻게 연결돼 있나"* 수준의 질문에 답한다. 기존 RRF+리랭커 검색이 잘 못하는 영역을 **보완**한다 — 대체가 아니다.

**핵심 설계**: 개념 레이어는 100% Claude Code 셀프추출이다 — API 호출도 LLM 비용도 없다. Claude Code가 `graph_list_chunks`로 청크를 읽고, 스스로 개념/관계를 추출해 `graph_store_concepts`로 SQLite에 저장한다. `sync_concept_notes`가 SQLite를 `data/concepts/<slug>.md` 볼트 노트로 투영하고, **열람은 이 노트 파일을 직접 읽는 것**이 기본 경로다 (별도 조회 도구 없음).

| | 기존 (ES + RRF + 리랭커) | 그래프 RAG |
|---|---|---|
| 잘하는 것 | "DI란?", "BM25 공식은?" 같은 **구체 질의** | "DI·IoC·Bean·Container가 어떻게 얽혀?" 같은 **관계/전역 질의** |
| 데이터 단위 | 청크 (500토큰) | 개념(entity) + 관계(relation) |
| 응답 재료 | 본문 청크 | `data/concepts/` 노트 (산문 + 관계 링크 + 출처) |
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
| `concept_edges` | 개념 간 관계 (relation, weight, evidence_count) |
| `concept_mentions` | 개념이 등장한 `doc_id`/`chunk_index` |
| `concept_curation` | 개념 큐레이션(real/vocab) + 증류 산문 |
| `graph_runs` | 그래프 빌드 실행 기록 |

스키마 정의는 `src/pkb/graph/schema.py`, CRUD는 `src/pkb/graph/store.py`.

### 개념 정규화 (dedup)
1. **Slug 일치**: `dependency injection` == `Dependency Injection`
2. **Alias 일치**: "DI" → 기존 "Dependency Injection" concept에 매핑
3. **임베딩 유사도** (≥ `GRAPH_DEDUP_THRESHOLD`, 기본 0.88): 기존 개념과 의미 매칭 → merge
4. 새 개념이면 insert, 기존이면 `mention_count += 1`, alias 추가

### 관계 엣지 누적
- 같은 (src, dst, relation) 조합 재등장 → `weight += 1`, `evidence_count += 1`
- 각 언급 청크 → `concept_mentions`에 기록

---

## 파이프라인: 셀프추출 → 저장 → 노트 투영

### 1. `graph_list_chunks(category|doc_id, offset, limit)`
ES 청크를 페이지 단위 JSON으로 반환. Claude Code가 이 결과를 직접 읽고 아래 규칙으로 개념/관계를 추출한다.

- 개념: 구체적 명사구 (예: "Dependency Injection", "BM25"). 일반 단어/인명/지명 제외
- 관계 타입: `related_to` | `part_of` | `prerequisite_of` | `example_of` (필요 시 자유 라벨 허용)
- 청크당 개념 8개·관계 12개 이내

### 2. `graph_store_concepts(items_json)`
추출한 개념/관계 JSON을 SQLite에 upsert (정규화·alias·mention·edge 포함).

### 3. `sync_concept_notes(confirm_prune)`
SQLite → `data/concepts/<slug>.md` 노트로 단방향 투영 (결정적·멱등). 개념 엣지를 `[[위키링크]]`로 되써서 Obsidian 그래프뷰가 개념그래프를 그리게 한다. `<!-- pkb:auto:start/end -->` 마커 사이만 재생성하므로 마커 밖 사용자 산문은 보존된다. 노트→SQLite 역승격은 없음(SQLite가 항상 SSOT). 구현: `src/pkb/graph/notes.py`.

### 4. 열람
개념 관계 질문에는 `data/concepts/<slug>.md`를 `search_knowledge`/`get_document`로 읽는다. 노트에는 설명, 관계(`part_of`/`prerequisite_of`/`related_to`), 출처 문서 링크가 담겨 있다.

---

## CLI 보조 명령

```bash
uv run pkb graph stats        # 개념/엣지/멘션/문서/별칭/run 수 통계
uv run pkb graph sync-notes   # SQLite → data/concepts/ 노트 동기화
uv run pkb graph sync-notes --yes   # 대량 정리(21개 이상) 확인 생략
```

일괄 API 빌드나 export CLI는 없다 — 구축과 열람 모두 MCP-first(Claude Code 셀프추출 + 노트 파일 읽기) 경로 하나로 통일했다.

---

## 왜 SQLite인가

- 단일 파일, 설치 불필요, 백업 쉬움
- 수천~수만 노드 규모에서는 충분히 빠름
- Python `sqlite3`만 쓰면 외부 라이브러리 최소화

## 왜 API 추출을 없앴는가

Claude Code가 이미 청크를 읽을 수 있는 세션이므로, 별도 LLM API 호출(과 비용·키 관리)은 중복이었다. `graph_list_chunks`/`graph_store_concepts` 두 도구만으로 Claude Code 세션 자체가 추출기로 동작한다.
