# CLI 사용법 (대체 인터페이스)

> PKB의 **기본 사용 방법은 [MCP 연동](mcp.md)**입니다. 이 문서는 Claude Code 없이 직접 CLI로 작업하거나, 웹 UI로 쓰고 싶을 때의 대체 경로를 설명합니다.

## 사전 준비

`.env` 파일에 Claude API 키 설정 (CLI 대화/Web UI 에이전트에 필요):

```bash
cp .env.example .env
# ANTHROPIC_API_KEY 입력
```

MCP만 사용할 거면 `.env` 설정은 불필요합니다 (Claude Code가 LLM 역할).

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

## 외부 파일 변환 + 저장 (PDF/DOCX/PPTX/XLSX/HTML → .md)

원본은 어느 위치든 상관없습니다. 변환된 `.md`가 `data/<category>/`에 저장되고 자동 인제스트됩니다:

```bash
# 카테고리 자동 분류 (Claude Haiku가 내용 기반으로 판단)
uv run pkb convert ~/Downloads/paper.pdf
# → "카테고리 자동 분류 중... → study" 후 data/study/paper.md에 저장

# 명시적 카테고리 지정
uv run pkb convert ~/Downloads/paper.pdf --category study

# 자동 인제스트 끄기 (검토 후 수동 인제스트)
uv run pkb convert ~/Downloads/doc.docx --ingest false
uv run pkb add data/<분류된-카테고리>/doc.md   # 나중에 인제스트
```

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

data/와 OBSIDIAN_PATH 전체를 새로 인덱싱합니다.

---

## Obsidian 볼트 연동

`.env`에 `OBSIDIAN_PATH=/absolute/path/to/vault` 설정 후:

```bash
# 최초 설정: ES 인덱스 생성 + Obsidian 전체 초기 인제스트
uv run pkb init

# 실시간 동기화: data/와 Obsidian 볼트 변경사항을 감지해서 자동 재인제스트
uv run pkb watch
```

Obsidian 볼트의 파일은 `category=obsidian`, `doc_id`는 `obsidian/<상대경로>` 로 저장됩니다.

`pkb watch`는 포그라운드 프로세스입니다 (Ctrl+C로 종료). 감지되는 이벤트:
- 생성/수정: 해당 파일만 재인제스트
- 삭제: ES에서 해당 문서 제거
- 이동: 원본 삭제 + 신규 인제스트

---

## CLI 에이전트 대화 (MCP 없이 단독 사용)

`.env`의 `ANTHROPIC_API_KEY`가 설정되어 있으면 내장 LangGraph 에이전트로 대화 가능:

```bash
uv run pkb chat
```

```
질문> 내가 공부한 RAG 관련 내용 정리해줘
질문> study 폴더 내용 중 벡터 검색 관련한 것만 추려서 정리해줘
```

에이전트가 `search_knowledge`, `write_file`, `list_documents` 도구를 자동 호출합니다.

> 대부분의 경우 **Claude Code + MCP** 조합이 더 편리합니다 ([docs/mcp.md](mcp.md) 참조).

---

## Web UI

```bash
uv run pkb serve
# http://localhost:8000
```

브라우저에서 문서 목록, 검색, 채팅을 할 수 있습니다.

기본적으로 `127.0.0.1`에만 바인딩됩니다. LAN에 노출하려면:

```bash
uv run pkb serve --host 0.0.0.0
```

---

## 프로젝트 구조

```
├── docker-compose.yml       # ES + nori 컨테이너
├── Dockerfile.es            # ES 이미지 + nori 플러그인
├── pyproject.toml
├── .env.example             # 환경 변수 템플릿
│
├── data/                    # 개인 문서 (gitignored)
│
└── src/pkb/
    ├── mcp_server.py        # MCP 서버 (기본 사용 방법)
    ├── cli.py               # CLI 커맨드
    ├── agent.py             # LangGraph ReAct 에이전트 (CLI/Web용)
    ├── tools.py             # 에이전트 도구
    ├── config.py            # 설정 관리
    ├── ingest.py            # 파싱, 청킹 (markitdown)
    ├── embeddings.py        # sentence-transformers 임베딩
    ├── store.py             # Elasticsearch CRUD, 인덱스 관리
    ├── retrieve.py          # 하이브리드 검색 (BM25 + kNN)
    ├── web.py               # FastAPI 웹 서버
    └── templates/           # Jinja2 HTML 템플릿
```

---

## 설정

`.env` 파일:

```
ANTHROPIC_API_KEY=sk-ant-...    # CLI 에이전트/Web UI용 (MCP만 쓸 거면 불필요)
ES_HOST=http://localhost:9200   # Elasticsearch 호스트 (기본값)
ES_INDEX=pkb_documents          # 인덱스 이름 (기본값)
OBSIDIAN_PATH=                  # (선택) Obsidian 볼트 절대경로
```

`pkb.config.Settings`의 다른 튜닝 옵션 (환경변수로 오버라이드 가능):

| 이름 | 기본값 | 설명 |
|------|--------|------|
| `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | sentence-transformers 모델 |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder 리랭커 모델 |
| `RERANK_ENABLED` | `true` | 리랭크 기본 사용 여부 |
| `FUSION` | `rrf` | 하이브리드 결합 방식 (`rrf` 또는 `native`) |
| `CANDIDATE_K` | `50` | RRF/리랭커 후보 수 |
| `EXPAND_CONTEXT` | `0` | N>0이면 검색 결과 전후 N 청크를 neighbors로 부착 (parent context) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 100 | 고정 크기 청킹 |

## 청킹 전략

- 비마크다운 파일(PDF/docx/pptx/xlsx/html)은 `markitdown`으로 마크다운 변환
- YAML frontmatter(있으면) 파싱하여 `title`/`tags` 추출
- H1~H3 헤딩 경계로 계층 분할 (각 청크에 `section_path` 기록: `대주제 > 소주제 > 세부`)
- 섹션 내부에서 고정 크기 500토큰 + 100토큰 오버랩
- 단락(`\n\n`) 경계 존중

## 검색 로그

모든 검색 호출은 `data/.logs/search.jsonl`에 기록됩니다 (query, category, fusion, reranked, results).

```bash
tail data/.logs/search.jsonl | jq .
```
