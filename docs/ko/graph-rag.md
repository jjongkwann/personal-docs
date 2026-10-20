# SQLite Graph RAG

## 목적과 경계

**목적**: *"내 자료 전체의 개념이 어떻게 연결돼 있나"* 수준의 질문에 답한다. 기존 RRF+리랭커 검색이 잘 못하는 영역을 **보완**한다 — 대체가 아니다.

**핵심 설계**: 추출은 명시적으로 실행한다. Claude Code가 `graph_list_chunks`로 청크를 읽고 개념/관계를 추출해 `graph_store_concepts`로 저장하거나, `pkb graph rebuild-evidence-local`이 로컬 Ollama 모델로 같은 pending 루프를 실행한다. `graph_explain`, `graph_path`, `graph_query`, `graph_affected`는 SQLite를 직접 조회하고 `graph_map`은 오프라인 HTML 지도를 만든다.

| | 기존 (ES + RRF + 리랭커) | 그래프 RAG |
|---|---|---|
| 잘하는 것 | "DI란?", "BM25 공식은?" 같은 **구체 질의** | "DI·IoC·Bean·Container가 어떻게 얽혀?" 같은 **관계/전역 질의** |
| 데이터 단위 | 청크 (500토큰) | 개념(entity) + 관계(relation) |
| 응답 재료 | 본문 청크 | SQLite 개념·관계·큐레이션 산문·출처 근거 |
| 빌드 시점 | 인제스트 즉시 | 명시적 MCP 추출 또는 로컬 Ollama 재구축 |

**안 하는 것**: 자동 전체 그래프 빌드(opt-in만), Neo4j 등 풀 그래프 DB, GNN 임베딩, 대화 히스토리 기반 자동 업데이트.

---

## 저장 구조

**파일 위치**: `data/.graph/pkb_graph.sqlite` (gitignored)

주요 테이블:

| 테이블 | 역할 |
|--------|------|
| `concepts` | 정규화된 개념 노드 (name, slug, kind `concept_type`, description, embedding, mention_count) |
| `concept_aliases` | DI → Dependency Injection 같은 별칭 |
| `documents` | ES `doc_id`와 연결되는 문서 노드 |
| `concept_edges` | 개념 간 관계의 집계 투영 (relation, weight, evidence_count) |
| `concept_edge_evidence` | 관계를 추출한 `doc_id`/`chunk_index`별 근거 |
| `concept_mentions` | 개념이 등장한 `doc_id`/`chunk_index` |
| `concept_curation` | 개념 큐레이션(real/vocab) + 증류 산문 |
| `extracted_chunks` | 추출 완료 마커 `(doc_id, chunk_index)` → `content_hash`, `input_hash` |
| `graph_meta` | key/value 마커 — 1회성 스키마 마이그레이션, `tbox_version`, `edge_evidence_rebuild` staging 플래그 |

증분 추출은 `extracted_chunks`가 기준이다. 현재 ES 청크와 마커의 두 해시 중 하나라도 다르면
pending이 된다. `content_hash`는 본문을, `input_hash`는 본문에 더해 `doc_id`, `chunk_index`,
제목, 절 경로, 카테고리를 추적하므로 출처 메타데이터 변경도 재추출 대상이다. 재추출은
**그 청크의 멘션과 관계 evidence를 교체**한다. `input_hash`가 없는 기존 마커(`chunk_index`가
NULL인 구마커 포함)와 그래프 데이터는 보존하지만, 현재 입력으로 재추출하기 전까지 pending이다.
엣지 `weight`/`evidence_count`는 `concept_edge_evidence`의 실측 행 수에서 계산하므로 같은 청크를
재호출해도 부풀지 않는다. 문서 삭제·청크 이동·내용 변경 시 해당 evidence도 함께 정리된다.

스키마 정의는 `src/pkb/graph/schema.py`, CRUD는 `src/pkb/graph/store.py`.

### 개념 정규화 (dedup)
추출 시 같은 카테고리/namespace 안에서 정규화된 slug가 일치하면 기존 개념을 재사용한다
(`dependency injection` == `Dependency Injection`). 제안된 별칭은 기록하되 충돌은 보고하고,
자동 추출에서는 약어·동음어 오병합을 막으려고 별칭·임베딩 유사도 매칭을 사용하지 않는다.
남은 중복은 검토 후 명시적으로 병합한다. `mention_count`는 `concept_mentions` 실측치로
재계산해 같은 청크를 재추출해도 부풀지 않는다.

dedup을 통과해 이미 별도 노드로 쪼개진 중복(표기 변형 등)은 `store.merge_concepts(conn,
winner_slug, loser_slugs)`로 사후 병합한다 — 엣지·mention·별칭·산문을 승자로 승계하고
loser 행은 같은 SQLite 트랜잭션에서 삭제한다. 주의: "MCP Server"처럼
상위 개념의 **구성요소**는 표기 변형이 아니므로 병합 금지.

### 큐레이션 기준
검색 결과에 붙이는 관련 개념은 **`concept_curation.label='real'`이면서 관계(엣지)를 1개
이상 가진 개념**을 우선한다. 고아(관계 0) 개념은 SQLite에 남지만 탐색 가치가 낮아 부착
대상에서 제외하며, `vocab` 개념도 기본 그래프 시드에서 제외한다. 큐레이션 테이블이
비어 있으면 전 개념으로 폴백한다.

### 관계 엣지 집계
- 같은 청크의 (src, dst, relation) 재호출 → evidence 멱등 upsert (count 불변)
- 서로 다른 청크의 같은 관계 → `weight`/`evidence_count`를 evidence 수로 집계
- 각 언급 청크 → `concept_mentions`에 기록

---

## 온톨로지 (TBox)

`src/pkb/graph/ontology.py`는 모든 개념과 관계가 맞아야 하는 클래스와 관계를 정의한다. 데이터만
있다(`TBOX_VERSION = "pkb-tbox/1"`, `graph_meta.tbox_version`에 기록). 저장 시 검사, 검증, 질의 시
추론, 추출 프롬프트, RDF 내보내기가 모두 이 정의를 읽는다.

### 개념 종류 (kind)
개념마다 `concepts.concept_type`에 leaf kind가 정확히 하나 있고 leaf kind끼리는 서로 배타적이다.
NULL은 아직 분류하지 않은 레거시 개념이다. 한국어·영어 한 줄 정의는 `ontology.KINDS`에 있다.

| 그룹 | kind |
|---|---|
| Abstract | `field`, `problem`, `principle` |
| Method | `technique`, `algorithm`, `pattern` |
| Artifact | `software`, `model`, `dataset`, `protocol` |
| Measure | `metric` |

### 관계

| 관계 | 역방향 (표시용) | 특성 | 시그니처 (src → dst) |
|---|---|---|---|
| `is_a` | `has_subtype` | 전이, 비반사, 비순환 | 두 끝의 kind가 같음 |
| `part_of` | `has_part` | 전이, 비반사, 비순환 | 같은 그룹, 또는 dst가 `field` |
| `prerequisite_of` | `requires` | 전이, 비반사, 비순환 | any → any |
| `uses` | `used_by` | 비반사 | Method\|Artifact → Method\|Artifact\|Measure |
| `implements` | `implemented_by` | 비대칭, 비반사 | software\|model → algorithm\|technique\|pattern\|protocol |
| `addresses` | `addressed_by` | 비반사 | Method\|Artifact → problem |
| `alternative_to` | 자기 자신 | 대칭, 비반사 | 같은 그룹 |
| `related_to` | 자기 자신 | 대칭, 비반사 | any → any. 더 구체적인 관계가 없을 때만 |

`example_of`는 어휘에서 빠졌고(기존 행은 `is_a`로 이관) 자유 라벨은 거부한다. `ontology.describe()`는
TBox 전체를 JSON으로, `ontology.allowed_relations(src_kind, dst_kind)`는 두 kind에 시그니처가 맞는
관계를 돌려준다.

### 저장 시 규칙
`store.add_edge`가 쓰기 전에 모든 관계를 검사하므로 `graph_store_concepts`와 로컬 Ollama 재구축이
같은 규칙을 따른다.

- 관계 타입은 카탈로그에 있어야 하고 자기 자신을 가리킬 수 없다.
- 두 끝에 모두 kind가 있으면 시그니처가 맞아야 한다. 한쪽이라도 미분류면 저장하고 unchecked로 센다.
- 비순환 관계는 dst에서 같은 관계를 따라 src에 닿으면(순환이 생기면) 거부한다.
- 대칭 관계는 `src_id < dst_id` 한 방향으로만 저장한다.

`graph_store_concepts` 결과는 거부한 관계를 한 줄에 하나씩 JSON으로 싣는다. `code`
(`unknown_relation`, `self_loop`, `signature`, `cycle`), `reason`, 두 kind, `allowed_relations`가
들어 있다. 같은 payload의 다른 유효 항목·관계는 그대로 저장한다. 개념에는 `kind`를 붙인다. 유효한
kind가 없는 새 개념은 거부하고(`allowed_kinds`와 함께 보고), 미분류 기존 개념에는 payload의 kind를
채우며, 다른 kind가 이미 있는 기존 개념은 저장된 kind를 유지하고 경고한다. kind 변경은 명시적
분류로만 한다.

### 이관
그래프에 연결할 때마다 `concept_type` 열이 없으면 더하고 `tbox_version`을 기록한다. 쓰기 연결은 TBox에
맞지 않는 관계 키도 정규화한다. `src_id > dst_id`로 저장된 `related_to`/`alternative_to` 행을 반대 방향으로
옮기고(근거는 청크별로 합치고 confidence는 최댓값, 집계 관계는 근거에서 다시 계산), `example_of` 행을
`is_a`로 옮긴다. 양방향으로 저장돼 있던 쌍만 하나로 합쳐진다. 한 번만 도는 이관이 아닌 이유는, 이 변경
전에 시작한 프로세스(오래 도는 추출기 등)가 같은 DB에 역방향 행을 계속 쓰기 때문이다. 검사 비용은 수 ms다.
읽기 전용 연결은 이 단계를 건너뛰고 쓰기 잠금을 잡지 않으므로, 다른 프로세스가 쓰는 동안에도 그래프 읽기가 된다.
모르는 라벨은 그대로 두고 검증 보고서에 나온다.

### 분류
`services.list_unclassified_concepts(conn, offset=0, limit=50)`는 미분류 개념을 언급 수 순으로 이름·
설명·별칭과 함께 페이지 단위로 준다. `services.classify_concepts(conn, [{"slug", "kind"}])`는 kind를
지정하거나 바꾼다. kind가 하나라도 틀리면 배치 전체를 거부한다. 결과에는 분류 뒤 시그니처를 어기게
된 관계가 들어 있어 고칠 수 있다.

### 검증
`validate.validate_graph(conn)`는 항목별 개수와 제한된 slug 샘플을 돌려준다.

- **hard**: 모르는 관계 라벨, 자기 루프, 시그니처 위반, 비순환 관계의 순환(slug 경로), 두 방향으로
  저장된 대칭 쌍, 근거 없는 관계.
- **soft**: 미분류 개념, unchecked 관계, `is_a`/`part_of` 경로로 이미 이어진 개념 사이의
  `related_to`/`alternative_to`(SKOS 무결성 조건 S27), 같은 관계의 더 긴 경로로 이미 따라 나오는
  전이 관계, 전체 관계 중 `related_to` 비율.

### 추론
추론 관계는 질의 시점에 계산한다. 저장하지 않고 근거로 세지 않는다.
`query.transitive_closure(conn, concept, relation, direction="up"|"down")`는 `is_a`, `part_of`,
`prerequisite_of`를 따라가 직접 단언되지 않은 관계만 돌려준다. 각 결과에는 `inferred: true`,
`hops`, 전제 경로 `via`가 있다. `up`은 저장 방향이다. `graph_explain`은 `inferred`
목록(outbound 폐포), inbound 관계의 `inverse_relation`, 개념과 관계 끝점의 `kind`를 더한다. 방향
탐색(`graph_affected`, 방향 있는 `graph_path`)은 대칭 관계를 양쪽으로 따라간다.

### RDF 내보내기
`export.export_rdf(conn, path, fmt="turtle"|"trig", base="urn:pkb:")`가 쓰는 내용:

- OWL 2 TBox: kind마다 그룹과 루트 클래스 `concept`(`skos:Concept`의 하위 클래스) 아래의 클래스,
  leaf kind 전체에 `owl:AllDisjointClasses`, 관계마다 특성을 붙인 object property, 역방향 이름의
  `owl:inverseOf`, 제한이 있는 domain/range, `skos:broader`(`is_a`는 `isothes:broaderGeneric`,
  `part_of`는 `isothes:broaderPartitive`도) 또는 `skos:related`로의 `rdfs:subPropertyOf`. 버전 IRI는
  `<base>tbox/1`.
- 개념마다 kind 클래스의 `skos:Concept`(하나의 `skos:ConceptScheme`), `prefLabel`, `altLabel`(별칭),
  `definition`, 그리고 단언된 관계.
- TriG는 추론한 전이 폐포를 이름 있는 그래프 `<base>graph/inferred`에 따로 둔다.

개념 IRI는 `<base>concept/<퍼센트 인코딩한 slug>`이고 표시 이름은 쓰지 않는다. OWL 2 DL은 비반사
특성을 비전이 속성에만 허용하므로 전이 관계에는 대신 주석 `pkbo:acyclic true`를 달고, 같은 kind·같은
그룹 조건은 `pkbo:signature`에 남긴다.

### 도구와 명령

| MCP 도구 | CLI | 권한 | 용도 |
|---|---|---|---|
| `graph_ontology` | `pkb graph ontology` | read | TBox JSON. 추출 전에 읽는다 |
| `graph_inferred` | `pkb graph inferred 개념 --relation is_a --direction up` | read | 전이 관계로 추론한 관계와 전제 경로 |
| `graph_validate` | `pkb graph validate` (hard 위반이 있으면 종료 코드 1) | admin | hard/soft 보고서. `pkb doctor`가 개수를 보여 준다 |
| `graph_classify` | — | admin | 미분류 개념 목록을 받은 뒤 `[{slug, kind}]`로 지정 |
| — | `pkb graph export --out graph.ttl [--format trig]` | admin (셸 전용) | 그래프 전체 RDF 파일 |

접근 정책이 있으면 읽기 도구는 읽을 수 있는 근거로 이뤄진 하위 그래프만 본다([접근 제어](access-control.md)).
그래서 추론도 숨겨진 전제를 거치지 않는다.

---

## 파이프라인: 셀프추출 → 저장 → 네이티브 조회

### 1. `graph_list_chunks(category|doc_id, offset, limit, pending_only)`
ES 청크를 `content_hash`와 `input_hash`를 포함한 페이지 단위 JSON으로 반환한다.
`pending_only=True`면 미추출·입력 변경 청크만 반환한다. Claude Code가 이 결과를 직접 읽고
아래 규칙으로 개념/관계를 추출한다. 추출 전 `graph_list_concepts`로 기존 어휘를 확인해
겹치는 개념은 기존 name/slug를 재사용한다.

- 개념: 구체적 명사구 (예: "Dependency Injection", "BM25"). 일반 단어/인명/지명 제외
- 개념마다 TBox의 `kind` 하나 (새 개념은 필수)
- 관계 타입: TBox의 관계 8개. 두 kind에 시그니처가 맞는 가장 구체적인 관계를 고르고 `related_to`는
  마지막 수단으로만
- 청크당 개념 8개·관계 12개 이내

### 2. `graph_store_concepts(items_json)`
각 item은 개념이 없어도 목록에서 받은 `content_hash`와 `input_hash`를 그대로 전달한다.
저장 시 현재 ES 청크와 두 해시를 비교한 뒤 SQLite를 변경한다. 해시 누락·청크 삭제·입력
변경 항목은 개별 거부하고 보고하며, 같은 배치의 유효 항목은 저장한다. TBox를 어긴 개념·관계는
구조화된 이유와 함께 개별 거부한다(저장 시 규칙 참고).

### 3. 조회와 열람
단일 개념은 `graph_explain`, 최단 연결은 `graph_path`, 자연어 관계 질문은 의미 시드 기반
`graph_query`, 저장 방향 기준 하위 범위는 `graph_affected`로 조회한다. 모든 엣지에는 confidence와
제한된 `doc_id`/`chunk_index` evidence가 포함된다. 사람이 보는 그래프는 `graph_map`으로
오프라인 HTML을 만들고, 원문 내용이 필요한 질문은 `search_knowledge`를 사용한다.

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

일괄 API 빌드는 없다. 구축은 MCP-first로 유지하고, 조회는 SQLite 네이티브 그래프 도구, CLI,
오프라인 Evidence Map, RDF 내보내기(`src/pkb/graph/export.py`)를 함께 제공한다.

---

## 왜 SQLite인가

- 단일 파일, 설치 불필요, 백업 쉬움
- 수천~수만 노드 규모에서는 충분히 빠름
- Python `sqlite3`만 쓰면 외부 라이브러리 최소화

## 왜 API 추출을 없앴는가

Claude Code가 이미 청크를 읽을 수 있는 세션이므로, 별도 LLM API 호출(과 비용·키 관리)은 중복이었다. `graph_list_chunks`/`graph_store_concepts` 두 도구만으로 Claude Code 세션 자체가 추출기로 동작한다.
