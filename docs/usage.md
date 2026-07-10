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

`data/` 디렉터리 구조:

```
data/
├── about/       # 자기소개, 관심사
├── career/      # 경력, 기술 스택, 프로젝트
├── study/       # 공부 노트, 교재
└── writing/     # 초안, 노트
```

---

## 쓰기 규칙

Claude Code가 `write_file`로 새 문서를 저장하기 전에 지키는 규칙입니다.

- 저장 전 내용을 다듬으세요 — 읽을 수 있는 산문(제텔카스텐 톤), frontmatter(title·tags·필요시 expires_at) 포함.
- 배치 결정: ① 기존 주제 폴더 우선 (`list_documents`로 확인) ② 단발 조사는 `data/study/daily-research/`
  ③ 계획된 시리즈만 새 폴더 ④ 같은 주제가 2~3건 쌓이면 폴더로 승격. 날짜는 파일명이 아니라 frontmatter에.
- 개념노트(`data/concepts/`)는 SQLite 개념그래프의 자동 생성물입니다 — 직접 편집은 `auto:end` 마커
  아래에만 하세요 (마커 위는 다음 동기화 시 덮어씌워집니다).

---

## 외부 파일 변환 + 저장 (PDF/DOCX/PPTX/XLSX/HTML → .md)

원본은 어느 위치든 상관없습니다. 변환된 `.md`가 `data/<category>/`에 저장되고 자동 인제스트됩니다:

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

---

## Graph RAG 운영 CLI

개념 그래프 구축은 Claude Code가 MCP의 `graph_list_chunks`/`graph_store_concepts`로 직접 추출·저장합니다(API 호출 없음). CLI는 통계 조회와 노트 동기화만 제공합니다.

```bash
# 현재 그래프 통계
uv run pkb graph stats

# SQLite 개념그래프를 data/concepts/<slug>.md 노트로 동기화
uv run pkb graph sync-notes
uv run pkb graph sync-notes --yes   # 대량 정리 확인 생략
```

---

## Obsidian 볼트 연동

`.env`에 `OBSIDIAN_PATH=/absolute/path/to/vault` 설정 후:

```bash
# 최초 설정: ES 인덱스 생성 + Obsidian 전체 초기 인제스트
uv run pkb init
```

이후 볼트 변경사항은 `uv run pkb sync`(또는 MCP `sync_obsidian`)로 재조정합니다. 실시간 감시는 없습니다.

Obsidian 볼트의 파일은 `category=obsidian`, `doc_id`는 `obsidian/<상대경로>` 로 저장됩니다.

**볼트와 원본 공유(SSOT)**: `DATA_ROOT`를 볼트 하위 폴더(예: `<vault>/PKB`)로 지정하면 코퍼스 원본을 Obsidian에서 직접 열람·편집할 수 있습니다. doc_id는 그대로 `data/...`이며, 볼트 크롤(`init`/`reindex`/`sync_obsidian`)은 이 서브트리를 제외해 이중 인제스트를 막습니다.

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
    ├── ingest.py            # 파싱, 청킹 (markitdown)
    ├── embeddings.py        # sentence-transformers 임베딩
    ├── store.py             # Elasticsearch CRUD, 인덱스 관리
    ├── retrieve.py          # 하이브리드 검색 (BM25 + kNN)
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
| `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | sentence-transformers 모델 |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder 리랭커 모델 |
| `RERANK_ENABLED` | `true` | 리랭크 기본 사용 여부 |
| `CANDIDATE_K` | `20` | RRF/리랭커 후보 수. 기본값은 rerank 경로 벤치마크에서 `ck=50` 대비 latency 2.4x↓·품질 동일로 채택. 같은 벤치에서 RRF-only 경로도 `ck=20`이 nDCG/MRR 미세 우위. 더 큰 후보 풀이 필요하면 50으로 상향. |
| `EXPAND_CONTEXT` | `0` | N>0이면 검색 결과 전후 N 청크를 neighbors로 부착 (parent context) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 100 | 고정 크기 청킹 |
| `GRAPH_DB_PATH` | `data/.graph/pkb_graph.sqlite` | SQLite 개념 그래프 파일 |
| `GRAPH_DEDUP_THRESHOLD` | `0.88` | 개념 병합 임베딩 유사도 임계값 |

## 청킹 전략

- 비마크다운 파일(PDF/docx/pptx/xlsx/html)은 `markitdown`으로 마크다운 변환
- YAML frontmatter(있으면) 파싱하여 `title`/`tags`/`expires_at` 추출
- H1~H3 헤딩 경계로 계층 분할 (각 청크에 `section_path` 기록: `대주제 > 소주제 > 세부`)
- 섹션 내부에서 고정 크기 500토큰 + 100토큰 오버랩
- 단락(`\n\n`) 경계 존중

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

MCP 도구로도 동일:
- `archive_document(doc_id, reason="")` — Claude Code 대화에서 *"이 문서 아카이브해줘"*
- `restore_document(doc_id)` — 복구

### 3. 필터 오버라이드 — 아카이브도 보기

`search_knowledge(query, include_archived=True)` / `list_documents(include_archived=True)` 로 호출하면 아카이브/만료 문서도 포함됩니다. 기본값은 항상 **제외**.

### 4. doctor 로 상태 확인

```
pkb_documents: 10,677개 청크
  - obsidian: 7930
  - career: 1139
  - study: 1059
  - writing: 259
  - about: 62
  archived: 42  expired(still-visible): 3
```

- `archived`: 수동 아카이브된 청크 수 (archived_at 존재)
- `expired(still-visible)`: `expires_at`은 지났지만 아직 아카이브 안 된 것 — 검색엔 안 나오지만 DB엔 남아있음. `pkb purge-archived` 로 정리 가능.

## 검색 로그

모든 검색 호출은 `data/.logs/search.jsonl`에 기록됩니다 (query, category, fusion, reranked, results).

```bash
tail data/.logs/search.jsonl | jq .
```
