# PKB 아키텍처

PKB는 세 개의 층으로 구성됩니다:

1. **`data/`** — 개인 문서 원본 저장소 (Source of Truth)
2. **Elasticsearch** — 검색 엔진 (Runtime Index)
3. **LangChain + LangGraph** — 에이전트 (Reasoning Layer)

각 층은 독립적이어서 한 부분만 교체해도 나머지는 영향을 받지 않습니다 (예: ES → Qdrant로 교체해도 에이전트 코드 불변).

---

## 전체 다이어그램

```
[사용자 인터페이스]
  ├─ CLI (typer)         pkb chat, pkb query, pkb add/convert
  ├─ Web (FastAPI)       /chat, /query, / (index)
  └─ Claude Code (MCP)   search_knowledge, write_file, ...
           │
           ▼
[LangGraph 에이전트]  (src/pkb/agent.py)
  ├─ Claude (LangChain ChatAnthropic)
  ├─ ReAct 루프: 관찰 → 판단 → 행동
  ├─ 대화 히스토리 (HumanMessage / AIMessage)
  └─ tool_use로 도구 호출
           │
           ▼
[도구]  (src/pkb/tools.py, src/pkb/mcp_server.py)
  ├─ search_knowledge    ──┐
  ├─ list_documents      ──┤──→ Elasticsearch
  ├─ add_document        ──┘
  ├─ write_file          ────→ data/ 파일 생성
  └─ convert_and_ingest  ────→ markitdown + data/ 저장 + ES 인제스트
           │
           ▼
[하이브리드 검색]  (src/pkb/retrieve.py)
  ├─ BM25 (nori 분석기)
  ├─ kNN (dense_vector, cosine)
  ├─ RRF 결합 (Reciprocal Rank Fusion, k=60)
  └─ CrossEncoder 재순위 (선택, BAAI/bge-reranker-v2-m3)
           │
           ▼
[Elasticsearch]  (Docker 컨테이너: pkb-es)
  인덱스: pkb_documents
  각 청크 = 문서 1개:
    content       "청크 텍스트..."        (nori 한국어 분석기로 인덱싱)
    embedding     [0.1, 0.2, ...]       (384차원 dense_vector, 코사인 유사도)
    doc_id        "data/study/x.md"
    category      "study"
    chunk_index   3
    section_path  "대주제 > 소주제 > 세부"  (H1~H3 경로)
    title, tags, date_modified, language
           ▲
           │ 인제스트 파이프라인 (src/pkb/ingest.py):
           │   1. read_file_as_text       — md/txt는 그대로, 나머지는 markitdown
           │   2. chunk_markdown           — 500토큰 + 100 오버랩, ## 헤딩 우선
           │   3. embed                    — sentence-transformers (로컬)
           │   4. add_chunks               — ES bulk index (기존 청크 삭제 후 삽입)
           │
[data/ 폴더]  (원본, gitignored)
  about/    career/    study/    writing/
  .md  .txt  .pdf  .docx  .pptx  .xlsx  .html
```

---

## 1. `data/` 폴더 — 원본 저장소

사용자의 **개인 문서 원본**이 보관되는 곳. git에서 추적되지 않습니다 (`.gitignore`).

### 구조

```
data/
├── about/      # 자기소개, 관심사
├── career/     # 경력, 기술 스택, 프로젝트
├── study/      # 공부 노트, 교재
└── writing/    # 초안, 노트
```

### 지원 포맷

| 확장자 | 처리 방식 |
|-------|----------|
| `.md`, `.markdown`, `.txt` | 그대로 읽음 |
| `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html` | `markitdown`으로 마크다운 변환 |

### 원칙

- **단일 소스**: data/의 파일이 진실의 원본. ES 인덱스는 이 파일들의 검색용 사본
- **편집 가능**: `.md` 상태로 보관하면 추출 노이즈(PDF 사이드바, 페이지 번호 등) 수동 정리 가능
- **git 버전 관리 가능**: 개인 선택에 따라 git 추적하여 변경 이력 관리 가능
- **변경 시 재인제스트 필요**: 파일 수정 후 `pkb add` 또는 `pkb convert`로 ES 동기화

---

## 2. Elasticsearch — 검색 엔진

Docker 컨테이너(`pkb-es`)로 실행. 단일 인덱스 `pkb_documents`에 모든 청크가 저장됩니다.

### 저장 단위

파일 하나가 여러 청크(500토큰 + 100토큰 오버랩)로 분할되어 각 청크가 ES 문서 1개로 저장됩니다.

예: `data/study/1.1.1_NLP_과제.md`가 6개 청크면 ES에 6개 문서로 존재.

### 문서 스키마

```python
{
    "content": "청크 텍스트 내용...",       # nori 분석기로 인덱싱
    "embedding": [0.1, 0.2, ...],          # 384차원 벡터
    "source_path": "data/study/x.md",
    "category": "study",                    # about|career|study|writing|misc
    "doc_id": "data/study/x.md",
    "chunk_index": 0,
    "title": "추출된 제목",
    "tags": ["python", "backend"],
    "date_modified": "2026-04-16",
    "language": "ko",
}
```

### 하이브리드 검색 (retrieve.py)

두 가지 검색을 동시에 수행하고 ES가 점수를 자동 결합:

- **BM25 (키워드)**: `nori_tokenizer`로 한국어 형태소 분석 후 매칭. "FastAPI" 같은 고유명사/기술용어에 강함
- **kNN (벡터)**: sentence-transformers로 질의 임베딩 → 코사인 유사도 검색. "백엔드 프레임워크"처럼 의미적 유사성에 강함

```
BM25 결과 ─┐
           ├─→ ES가 점수 합산 → top-K 청크 반환
kNN 결과  ─┘
```

### 인덱스 갱신

- `pkb add <file>`: 파일의 기존 청크 삭제 → 새로 청킹/임베딩/삽입 (증분 빌드)
- `pkb delete <doc_id>`: 문서의 모든 청크 제거
- 내부적으로 `doc_id` 메타데이터로 매핑 추적

---

## 3. LangChain + LangGraph — 에이전트 (Reasoning Layer)

### LangChain (도구 정의)

- `ChatAnthropic`: Claude API 래퍼
- `@tool` 데코레이터: Python 함수를 LLM이 호출 가능한 도구로 노출

**등록된 도구** (`tools.py`):

| 도구 | 역할 |
|------|------|
| `search_knowledge` | ES 하이브리드 검색 |
| `write_file` | `data/` 하위에 파일 작성 (.md만) |
| `list_documents` | 저장된 문서 목록 조회 |

### LangGraph (에이전트 루프)

`create_react_agent`가 ReAct 패턴을 구현:

```
사용자 메시지
  ↓
[관찰] Claude가 현재 상태와 필요한 정보 평가
  ↓
[판단] "검색이 필요한가? 어떤 도구를 어떤 인자로 호출할까?"
  ↓
[행동] 도구 호출 (tool_use)
  ↓
[관찰] 도구 결과를 컨텍스트에 추가
  ↓
[판단] "답변하기 충분한가? 추가 검색이 필요한가?"
  ↓
↑────────────── 반복 (반성 루프) ──────────────
  ↓
최종 답변 생성
```

### 시스템 프롬프트 (`agent.py`)

Claude에게 언제 어떤 도구를 써야 하는지 지시:

- 질문 답변 → `search_knowledge` 먼저
- 파일 작성 요청 → 검색 후 `write_file`
- 문서 확인 요청 → `list_documents`
- 검색 결과 부족 → 다른 키워드/카테고리로 재검색

### 대화 히스토리

`agent.py`의 `chat()` 함수가 `HumanMessage`/`AIMessage` 리스트로 세션별 히스토리 유지. CLI에서는 프로세스 종료까지, 웹에서는 `session_id`별 in-memory 딕셔너리로 관리.

---

## 4. MCP 서버 (별도 통합 층)

`mcp_server.py`는 Claude Code가 PKB 도구를 직접 호출할 수 있도록 MCP 프로토콜로 노출합니다. **LangGraph 에이전트를 거치지 않고** Claude Code 자체가 에이전트 역할을 합니다.

```
Claude Code → MCP (stdio) → mcp_server.py → 직접 ES/파일 조작
```

노출되는 도구:
- `search_knowledge`
- `write_file`
- `list_documents`
- `add_document`
- `convert_and_ingest`

---

## 실제 질의 흐름 예시

### 예시 1: 저장된 자료 기반 요약 생성

```
사용자: "저장된 BM25 관련 내용 정리해서 data/writing/bm25.md에 저장해줘"
   │
   ▼
LangGraph 에이전트
   ├─ [판단] "BM25 관련 study 노트 검색 필요"
   ├─ [행동] search_knowledge(query="BM25", category="study")
   │     └─→ ES가 BM25 + kNN으로 top-5 청크 반환
   │           • data/study/1.2.10_BM25의_확률적_관련_모델_배경.md #2
   │           • data/study/3.1.2_BM25_vs_밀집_정밀도.md #1
   │           • ...
   ├─ [판단] "충분한 자료 확보, 요약 생성"
   ├─ [생성] Claude가 검색 결과를 읽고 요약 작성
   ├─ [행동] write_file("data/writing/bm25.md", "# BM25 정리\n...")
   │     └─→ data/ 폴더에 새 .md 파일 생성
   └─ [답변] "data/writing/bm25.md에 저장했습니다"
```

### 예시 2: PDF 논문 추가 후 검색

```
1. pkb convert ~/Downloads/paper.pdf --category study
    ├─ markitdown이 PDF → .md 변환
    ├─ data/study/paper.md 저장
    └─ 자동 인제스트 (청킹 → 임베딩 → ES)

2. pkb chat
    질문> paper에서 제안한 핵심 방법이 뭐야?
    └─ 에이전트가 search_knowledge("paper 핵심 방법", "study") 호출
        └─ ES가 paper.md의 관련 청크 반환
        └─ Claude가 답변 생성
```

---

## 왜 이 구조인가

| 구성요소 | 역할 | 대안이 있을 때 이 선택을 한 이유 |
|---------|------|------------------------------|
| `data/` 원본 | 편집 가능, 영구 저장 | 단일 소스로 진실 유지, git 추적 가능 |
| Elasticsearch | 고속 검색 (키워드+의미) | 한국어 형태소 + 벡터 검색을 단일 쿼리로 지원 |
| LangChain | LLM·도구 추상화 | Claude API 직접 호출 없이 도구 체이닝 |
| LangGraph | ReAct 루프 | 다단계 추론 (검색 → 반성 → 재검색 → 답변) 자동 관리 |
| MCP | Claude Code 직접 통합 | 별도 CLI 없이 Claude Code 대화에서 PKB 사용 |

---

## 교체 가능 지점

- **ES → Qdrant/Chroma/pgvector**: `store.py`와 `retrieve.py`만 교체. 에이전트는 불변
- **sentence-transformers → OpenAI 임베딩**: `embeddings.py`의 `embed()` 함수만 교체
- **Claude → 다른 LLM**: `agent.py`의 `ChatAnthropic` → 다른 LangChain 통합체로 교체
- **markitdown → docling/unstructured**: `ingest.py`의 `read_file_as_text()` 교체
