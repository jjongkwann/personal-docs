# CLI 사용법 (대체 인터페이스)

> PKB의 **기본 사용 방법은 [MCP 연동](mcp.md)**입니다. 이 문서는 Claude Code 없이 직접 CLI로 색인·검색·유지보수할 때의 대체 경로를 설명합니다.

## 사전 준비

API 키가 필요 없습니다. `OBSIDIAN_PATH`처럼 로컬 경로 설정이 필요할 때만 `.env`를 사용합니다:

```bash
cp .env.example .env
```

---

## 문서 인제스트

지원 포맷: `.md`, `.txt`, `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`

```bash
# 단일 파일
uv run pkb add data/about/bio.md
uv run pkb add data/study/paper.pdf

# 디렉터리 전체 (재귀, 지원 포맷만 자동 선별)
uv run pkb add data/study/

# 태그 지정
uv run pkb add data/study/ --tags "python,backend,fastapi"
```

`data/` 디렉터리 구조 — **최상위 폴더명이 곧 카테고리이며 동적입니다**(폴더 추가 = 카테고리 추가):

```
data/
├── study/       # 공부 노트, 논문
├── career/      # 경력, 기술 스택, 프로젝트
├── writing/     # 초안, 노트
├── about/       # 자기소개, 관심사
└── ...          # 폴더 추가 = 카테고리 추가 (예: news, journal, 운동)
```

색인 제외: `_review/`, `_trash/`, `_materials/`, `_archive/`(검토 대기·폐기·중복·보관 원본),
`_origin/`(외부 원본 보관소 — 소화 노트만 색인, 원본은 근거 확인용), `.`으로 시작하는
폴더 전체(도구 산출물), `data/_concepts/`(개념노트 — SQLite→노트 투영본이라 ES 중복 색인 안 함).

---

## 쓰기 규칙

Claude Code가 `write_file`로 새 문서를 저장하기 전에 지키는 규칙입니다.

- 저장 전 내용을 다듬으세요 — 읽을 수 있는 산문(제텔카스텐 톤), frontmatter(title·tags·필요시 expires_at) 포함.
- 배치 결정: ① 기존 주제 폴더 우선 (`list_documents`로 확인) ② 단발 조사는 `data/study/daily-research/`
  ③ 계획된 시리즈만 새 폴더 ④ 같은 주제가 2~3건 쌓이면 폴더로 승격. 날짜는 파일명이 아니라 frontmatter에.
- 개념노트(`data/_concepts/`)는 SQLite 개념그래프의 자동 생성물입니다 — 직접 편집은 `auto:end` 마커
  아래에만 하세요 (마커 위는 다음 동기화 시 덮어씌워집니다).

---

## 외부 파일 변환 + 저장 (PDF/DOCX/PPTX/XLSX/HTML → .md)

원본은 어느 위치든 상관없습니다. 변환된 `.md`가 `data/<category>/`에 저장되고 자동 인제스트됩니다.
변환본 상단에는 provenance frontmatter(`source`·`converted_from`·`converted_at`)가 붙고, PDF는
페이지 마커(`## p.N`)가 보존됩니다:

```bash
# 기본값: misc 카테고리에 저장
uv run pkb convert ~/Downloads/paper.pdf
# → data/misc/paper.md에 저장

# 명시적 카테고리 지정 (권장)
uv run pkb convert ~/Downloads/paper.pdf --category study

# 자동 인제스트 끄기 (검토 후 수동 인제스트)
uv run pkb convert ~/Downloads/doc.docx --no-ingest --category study
uv run pkb add data/study/doc.md   # 나중에 인제스트
```

MCP 기본 경로에서는 `convert_and_ingest` 호출 시 Claude Code가 `category`를 직접 지정합니다.

---

## 검색

```bash
uv run pkb query "벡터 검색의 원리는?"
uv run pkb query "Python 프레임워크 경험" --category career --top-k 10
```

## 문서 관리

```bash
uv run pkb list
uv run pkb list --category study
uv run pkb delete data/study/rag-overview.md
```

---

## 인덱스 재생성

매핑 스키마가 바뀌었거나 (예: `section_path` 필드 추가) 인덱스가 꼬였을 때:

```bash
uv run pkb reindex           # 확인 프롬프트 있음
uv run pkb reindex --yes     # 바로 실행
```

데이터 코퍼스(DATA_ROOT)와 OBSIDIAN_PATH 전체를 새로 인덱싱합니다.

---

## 동기화 (재조정)

원본(데이터 코퍼스 + Obsidian 볼트)과 ES를 맞춥니다 — 업서트에 더해 **원본에서 사라진 유령 문서를 정리**합니다:

```bash
uv run pkb sync          # 대량 삭제(21개 이상)는 확인 프롬프트
uv run pkb sync --yes    # 확인 생략
```

설정이 곧 기대 상태입니다. 케이스별 동작:

| 상황 | 동작 |
|---|---|
| OBSIDIAN_PATH 없음 (처음부터) | obsidian 쪽 no-op |
| 설정돼 있음 | 델타 업서트 + 볼트에서 지운 파일 정리 |
| 없다가 설정함 | 볼트 전체 인제스트 |
| 설정했다가 제거함 | 잔존 `obsidian/*` 전체 삭제 제안 (확인 필요) |
| 볼트 경로 변경 | 새 볼트 인제스트 + 옛 문서 정리 |

`reindex`(전체 재구축)와 달리 `sync`는 델타만 처리하므로 일상 유지보수용입니다. `doctor`가 설정-인덱스 불일치(연동 껐는데 문서 잔존)를 경고합니다.

### 스테일 넛지 (SessionStart 훅)

`sync`가 성공하면 `data/.logs/last_sync.json`에 타임스탬프가 기록되고, `pkb stale`이 그 이후
수정된 원본 파일 수를 보고합니다 (`--quiet`는 stale일 때만 출력). 훅이 세션을 깨지 않도록
어떤 경우에도 exit 0입니다. mtime 스캔이라 삭제는 감지하지 않습니다 — 삭제 정리는 `sync`의
prune 몫입니다.

Claude Code `SessionStart` 훅으로 등록하면 세션 시작 시 sync 필요 여부를 알려줍니다
(`~/.claude/settings.json`, `<repo-path>`는 이 레포 절대경로로 교체):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --project <repo-path> pkb stale --quiet"
          }
        ]
      }
    ]
  }
}
```

---

## Graph RAG 운영 CLI

개념 그래프 구축은 Claude Code가 MCP의 `graph_list_chunks`/`graph_store_concepts`로 직접 추출·저장합니다(API 호출 없음). CLI는 통계·evidence 재구축 준비·노트 동기화를 제공합니다.

```bash
# 현재 그래프 통계
uv run pkb graph stats

# 구 append-only 관계를 evidence 기반으로 전량 재구축할 때만 실행
# 기존 그래프는 유지하고 staging evidence·추출 마커만 초기화
uv run pkb graph reset-evidence --yes

# 선택: 설치된 Ollama 모델로 pending 전량 자동 추출 (중단 후 재실행 가능)
uv run pkb graph rebuild-evidence-local --yes

# 수동 graph_list_chunks → graph_store_concepts 전량 처리 후 pending=0일 때 원자 전환
uv run pkb graph finalize-evidence --yes

# SQLite 개념그래프를 data/_concepts/<slug>.md 노트로 동기화
uv run pkb graph sync-notes
uv run pkb graph sync-notes --yes   # 대량 정리 확인 생략
```

---

## Obsidian 연동

**권장 구성**: `DATA_ROOT`를 볼트 하위 폴더(예: `<vault>/PKB`)로 지정합니다. 코퍼스 원본이
볼트 안에 있으므로 Obsidian에서 직접 열람·편집할 수 있고, doc_id는 위치와 무관하게 그대로
`data/...`입니다. 이 경우 아래 `OBSIDIAN_PATH` 크롤은 보통 필요 없습니다.

**`OBSIDIAN_PATH`(선택)**: `DATA_ROOT` 밖에 남겨둔 볼트 파일도 같이 검색하고 싶을 때만
설정합니다. `.env`에 `OBSIDIAN_PATH=/absolute/path/to/vault`를 설정하면:

```bash
uv run pkb init    # ES 인덱스 생성 + (설정 시) Obsidian 초기 인제스트
```

이후 볼트 변경사항은 `uv run pkb sync`(또는 MCP `sync_obsidian`)로 재조정합니다. 실시간 감시는
없습니다. 이 경로로 인제스트된 파일은 `category=obsidian`, `doc_id`는 `obsidian/<상대경로>`로
저장되며, `DATA_ROOT` 서브트리는 크롤에서 자동 제외되어 이중 인제스트가 없습니다.

---

## 프로젝트 구조

```
├── docker-compose.yml       # ES + nori 컨테이너
├── Dockerfile.es            # ES 이미지 + nori 플러그인
├── pyproject.toml
├── .env.example             # 환경 변수 템플릿
│
├── data/                    # 개인 문서 (gitignored, DATA_ROOT로 위치 변경 가능)
│
└── src/pkb/
    ├── mcp_server.py        # MCP 서버 (기본 사용 방법)
    ├── cli.py               # CLI 커맨드
    ├── config.py            # 설정 관리
    ├── ingest.py            # 파싱, 청킹 (PDF는 pdfminer 페이지 보존, 그 외 markitdown)
    ├── embeddings.py        # sentence-transformers 임베딩
    ├── rerank.py            # CrossEncoder 재순위
    ├── store.py             # Elasticsearch CRUD, 인덱스 관리
    ├── retrieve.py          # 하이브리드 검색 (BM25 + kNN + RRF)
    ├── report.py            # doctor 상태 리포트
    ├── search_log.py        # 검색 호출 JSONL 로깅
    └── graph/               # SQLite 기반 Graph RAG
```

---

## 설정

`.env` 파일:

```
ES_HOST=http://localhost:9200   # Elasticsearch 호스트 (기본값)
ES_INDEX=pkb_documents          # 인덱스 이름 (기본값)
OBSIDIAN_PATH=                  # (선택) Obsidian 볼트 절대경로
GRAPH_DB_PATH=data/.graph/pkb_graph.sqlite
GRAPH_DEDUP_THRESHOLD=0.88
```

`pkb.config.Settings`의 다른 튜닝 옵션 (환경변수로 오버라이드 가능):

| 이름 | 기본값 | 설명 |
|------|--------|------|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | sentence-transformers 모델 (1024d, `EMBEDDING_DIMS`와 함께 변경) |
| `EMBED_CONTEXT_PREFIX` | `true` | 임베딩 입력에 title·section_path prefix 포함 |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder 리랭커 모델 |
| `RERANK_ENABLED` | `false` | 리랭크 기본 사용 여부 |
| `CANDIDATE_K` | `20` | RRF/리랭커 후보 수. 기본값은 rerank 경로 벤치마크에서 `ck=50` 대비 latency 2.4x↓·품질 동일로 채택. 같은 벤치에서 RRF-only 경로도 `ck=20`이 nDCG/MRR 미세 우위. 더 큰 후보 풀이 필요하면 50으로 상향. |
| `EXPAND_CONTEXT` | `0` | N>0이면 검색 결과 전후 N 청크를 neighbors로 부착 (parent context) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 100 | 고정 크기 청킹 |
| `GRAPH_DB_PATH` | `data/.graph/pkb_graph.sqlite` | SQLite 개념 그래프 파일 |
| `GRAPH_DEDUP_THRESHOLD` | `0.88` | 개념 병합 임베딩 유사도 임계값 |

## 청킹 전략

- PDF는 `pdfminer`로 페이지 마커(`## p.N`) 보존 추출, 그 외 비마크다운(docx/pptx/xlsx/html)은 `markitdown`으로 마크다운 변환
- YAML frontmatter(있으면) 파싱하여 `title`/`tags`/`expires_at` 추출
- H1~H3 헤딩 경계로 계층 분할 (각 청크에 `section_path` 기록: `대주제 > 소주제 > 세부`)
- 섹션 내부에서 고정 크기 500토큰 + 100토큰 오버랩
- 단락(`\n\n`) 경계 존중
- 80자 미만(`MIN_CHUNK_CHARS`) 초소형 청크는 인접 청크에 병합 — 헤딩·링크만 있는 저정보 청크가
  리랭크 상위를 점령하는 것을 방지

## 문서 생명주기 (만료 + Soft Delete)

오래된 자료를 검색에서 자동으로 빼거나, 나중에 복구 가능하게 숨길 수 있습니다.

### 1. 만료 예정일 지정 (frontmatter)

```yaml
---
title: 2025 이력서
expires_at: 2026-01-01       # ISO 날짜. 지나면 자동으로 검색에서 제외.
tags: [resume, 2025]
---
```

`expires_at`은 `date`/`datetime`/ISO 문자열 모두 허용. 유효하지 않은 값은 경고 로그 후 무시됩니다. 지정된 날짜가 `now` 이전이면 `hybrid_search` 결과에서 자동 제외됩니다(ES lazy filter, 배치 없음).

### 2. 수동 아카이브 (soft delete)

```bash
# 개별 문서 숨기기 (검색 제외, 복구 가능)
uv run pkb archive data/career/old_resume.md --reason "newer version exists"

# 복구
uv run pkb restore data/career/old_resume.md

# 아카이브된 것 물리 삭제 (비가역! 명시 요청 시만)
uv run pkb purge-archived --before 2024-01-01
```

MCP 도구로도 가능:
- `archive_document(doc_id, reason="")` — Claude Code 대화에서 *"이 문서 아카이브해줘"*. `data/` 하위 `.md` 원본은 frontmatter에 `archived_at`을 기록 후 재인제스트해 reindex에도 상태가 유지됩니다. CLI `archive`도 같은 코어 경로를 사용합니다.
- `restore_document(doc_id)` — 복구 (frontmatter 기록이 있으면 제거)

### 3. 필터 오버라이드 — 아카이브도 보기

`search_knowledge(query, include_archived=True)` / `list_documents(include_archived=True)` 로 호출하면 아카이브/만료 문서도 포함됩니다. 기본값은 항상 **제외**.

### 4. doctor 로 상태 확인

```
인덱스 'pkb_documents': 13,033개 청크   # 예시 (2026-07 기준 실측)
  - study: 10598
  - career: 1871
  - writing: 222
  - news: 214
  - about: 81
  - 운동: 30
  - journal: 17
  archived: <N>  expired(still-visible): <N>
```

- `archived`: 수동 아카이브된 청크 수 (archived_at 존재)
- `expired(still-visible)`: `expires_at`은 지났지만 아직 아카이브 안 된 것 — 검색엔 안 나오지만 DB엔 남아있음. `pkb purge-archived` 로 정리 가능.
- 조치 후보는 doc_id로 나열됩니다 — 만료 미아카이브 문서, purge 후보(archived 30일 경과), 고아 개념(멘션 0). 판단·해소는 Claude 세션 몫.

## 검색 로그

모든 검색 호출은 `data/.logs/search.jsonl`에 기록됩니다 (query, category, fusion, reranked, results).

```bash
tail data/.logs/search.jsonl | jq .
```

## 검색 품질 평가 (eval)

골드셋(질문 → 정답 문서)을 4개 검색 모드(bm25 단독 / knn 단독 / rrf / rrf+rerank)로 돌려
recall@1/3/5/10과 MRR을 모드별로 비교합니다. 골드 문서가 top10 밖이면(miss) 실제 1위
doc_id를 함께 리포트합니다.

```bash
uv run pkb eval                            # 기본: <DATA_ROOT>/.eval/gold.jsonl
uv run pkb eval --gold path/to/gold.jsonl
```

### 골드셋 작성 지침

`data/.eval/gold.jsonl` — 라인당 한 항목:

```jsonl
{"query": "역색인에서 단어 빈도와 문서 길이를 함께 반영하는 점수식은?", "doc_id": "data/study/rag/1.2.10_BM25.md"}
```

- Claude Code 세션이 대상 문서를 직접 읽고 질문을 작성하게 하세요. 이때 **제목 단어 금지** —
  파일명·제목의 단어를 그대로 쓰면 BM25가 공짜로 맞혀 변별력이 사라집니다. 내용을 패러프레이즈한
  질문이어야 모드 간 차이가 드러납니다.
- `data/.eval/`은 별도 설정 없이 기존 규칙으로 자동 커버됩니다: `.`으로 시작하는 폴더는
  인제스트에서 제외되어 골드셋이 검색 대상에 섞이지 않고, `data/`는 gitignore라 커밋되지 않습니다.
