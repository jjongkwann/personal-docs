# Claude Code MCP 연동 (기본 사용 방법)

PKB의 기본 인터페이스는 **Claude Code + MCP**입니다. Claude Code가 에이전트 역할을 하고, PKB는 검색/파일 작성/인제스트/그래프 조회 도구를 MCP로 제공합니다.

CLI는 색인·검증·디버깅용 보조 인터페이스입니다. 평소 사용은 Claude Code 대화에서 처리합니다.

## 사전 요구사항

- Elasticsearch 실행: `docker compose up -d`
- Python 의존성 설치: `uv sync`
- 인덱스 초기화: `uv run pkb init`

API 키가 필요 없습니다 — Claude Code가 LLM 역할을 하는 100% 로컬 구성입니다.

## 등록

### 방법 A: `claude mcp add` CLI (권장)

프로젝트 디렉터리에서:

```bash
claude mcp add pkb -s user -- uv --directory "$(pwd)" run python -m pkb.mcp_server
```

`-s user` 플래그로 사용자 전역 설정에 등록되어, 어느 디렉터리에서든 Claude Code 실행 시 PKB 서버가 활성화됩니다.

### 방법 B: `~/.claude.json` 직접 편집

```json
{
  "mcpServers": {
    "pkb": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/personal-docs", "run", "python", "-m", "pkb.mcp_server"]
    }
  }
}
```

절대경로를 실제 경로로 교체합니다.

## 확인

Claude Code 재시작 후 `/mcp` 커맨드로 `pkb` 서버가 연결됐는지 확인합니다.

## 제공 도구

### 검색/문서 작업

| 도구 | 역할 |
|------|------|
| `search_knowledge` | 개인 지식 베이스 하이브리드 검색. BM25와 kNN을 분리 검색한 뒤 RRF로 결합, CrossEncoder로 재순위(기본 활성), 문서당 최대 2청크로 캡. 개념 그래프가 있으면 히트별 관련 개념 링크와 재검색용 개념 어휘를 함께 부착. `include_obsidian=False`로 코퍼스 밖(볼트 직속, `obsidian/` 접두 doc_id) 문서 제외 가능. 기본 검색이 부실하면 `query_variants`(RAG-Fusion 쿼리 변형, 최대 3개)로 변형 쿼리를 함께 검색해 RRF 병합 |
| `write_file` | `data/` 하위 `.md` 작성. 기본값으로 작성 직후 자동 인제스트 |
| `list_documents` | ES에 저장된 문서 목록. `date_modified` 내림차순으로 상위 `limit`(기본 50)개만 표시. `limit<=0`이면 전체 |
| `add_document` | `data/` 하위 파일 인제스트 (md/txt/pdf/docx/pptx/xlsx/html) |
| `convert_and_ingest` | 외부 PDF/DOCX/PPTX/XLSX/HTML을 `.md`로 변환해 `data/<category>/`에 저장하고 인제스트. 변환본 상단에 provenance frontmatter(`source`·`converted_from`·`converted_at`) 기록, PDF는 페이지 마커(`## p.N`) 보존 |
| `sync_corpus` | `data/` 코퍼스를 ES와 재조정: 업서트 + 코퍼스에 없는 문서 정리 (`confirm_prune=True`로 대량 삭제 승인) |
| `sync_obsidian` | Obsidian 볼트를 ES와 재조정: 업서트 + 볼트에 없는 문서 정리. 카테고리는 볼트 첫 폴더명에서 동적으로 파생(`data/`와 동일 규칙). OBSIDIAN_PATH 미설정 시 잔존 `obsidian/*` 전체 정리 제안 (`confirm_prune=True` 필요) |
| `get_document` | 문서 조회. 기본(`include_content=False`)은 메타(제목·카테고리·수정일) + 청크 목차만 반환. `chunk_range`(예: `"3-7"`)로 해당 구간만 전문, `include_content=True`면 전체 전문 |
| `reindex_document` | 특정 원본 문서를 다시 읽어 ES에 재인제스트 |
| `archive_document` | 문서를 soft delete(검색 제외, 복구 가능)로 아카이브. `data/` 하위 `.md` 원본은 frontmatter에 `archived_at` 기록 후 재인제스트(파일이 SSOT), 그 외는 ES에만 기록 |
| `restore_document` | 아카이브된 문서를 검색 대상으로 복구. frontmatter에 아카이브 기록이 있으면 제거 후 재인제스트 |
| `doctor` | ES 연결, 인덱스, 청크 수, 설정, 개념 그래프 통계 점검 + 조치 후보(만료 미아카이브·purge 대상·고아 개념) 나열 |

### Graph RAG

| 도구 | 역할 |
|------|------|
| `graph_list_concepts` | 그래프에 이미 있는 개념 어휘 조회 — 추출 전 호출해 겹치는 개념은 기존 name/slug 재사용 유도 |
| `graph_list_chunks` | 개념 추출 대상 청크를 페이지 단위로 조회. `pending_only=True`면 미추출·내용 변경 청크만 반환(증분 추출) |
| `graph_store_concepts` | Claude Code가 추출한 개념/관계 JSON을 SQLite 그래프 DB에 저장 |
| `graph_curate` | 개념 큐레이션 — 미큐레이션 목록 조회, real/vocab 라벨·산문 저장 |
| `graph_merge` | 표기 변형 개념을 대표 개념으로 병합 (엣지/멘션/별칭 승계) |
| `sync_concept_notes` | SQLite 개념그래프를 `data/_concepts/<slug>.md` 볼트 노트로 투영 + `_concepts/index.md` MOC(개념 어휘 카탈로그 진입점) 렌더 |

Graph RAG의 MCP-first 흐름은 `graph_list_chunks`로 청크를 읽고, Claude Code가 직접 개념과 관계를 추출한 뒤, `graph_store_concepts`로 저장하는 방식입니다. 저장 후 `graph_curate`로 real/vocab을 라벨링하고 표기 변형은 `graph_merge`로 병합한 뒤 `sync_concept_notes`로 투영합니다. 개념 관계를 열람할 때는 투영된 `data/_concepts/` 노트를 직접 읽습니다.

## 사용 예시

Claude Code에서 자연스럽게 대화하면 적절한 MCP 도구가 호출됩니다.

### 검색

- *"내 경력에서 Python 관련 프로젝트 찾아줘"* → `search_knowledge(category="career")`
- *"rag 자료 중 벡터 검색 관련 내용 있어?"* → `search_knowledge(category="rag")`
- *"DI IoC 의존성 주입 관련 노트 찾아줘"* → `search_knowledge(query="DI IoC 의존성 주입")`

### 외부 파일 추가

- *"이 PDF 논문을 study 카테고리로 넣어줘: `~/Downloads/paper.pdf`"* → `convert_and_ingest(category="study")`
- *"`~/Documents/notes.docx`를 writing 카테고리로 변환해서 넣어줘"* → `convert_and_ingest(category="writing")`
- *"자료 코퍼스를 최신 상태로 맞춰줘"* → `sync_corpus()` (업서트 + 유령 문서 정리)
- *"코퍼스 밖에 남겨둔 볼트 파일도 동기화해줘"* → `sync_obsidian()` (.env의 OBSIDIAN_PATH 사용)

`convert_and_ingest`의 `category`는 필수입니다. Claude Code가 파일명/사용자 지시/내용 맥락을 보고 지정합니다. 카테고리 = 코퍼스 최상위 폴더명(동적)이며, 기존 카테고리를 우선 사용하고 새 이름을 주면 새 카테고리 폴더가 생성됩니다. `study/payments`처럼 하위 폴더 경로 지정도 가능합니다(카테고리는 첫 폴더).

### 문서 관리

- *"저장된 career 문서 목록 보여줘"* → `list_documents(category="career")`
- *"이 문서 목차만 보여줘: `data/study/rag/xxx.md`"* → `get_document(doc_id="data/study/rag/xxx.md")` (기본은 메타+목차)
- *"이 문서 전체 내용 보여줘"* → `get_document(doc_id="...", include_content=True)`
- *"방금 수정한 문서 다시 인덱싱해줘"* → `reindex_document(doc_id="data/study/rag/xxx.md")`

### 파일 생성

- *"방금 검색한 내용을 요약해서 `data/writing/summary.md`에 저장해줘"* → `search_knowledge` + `write_file`
- *"찾은 내용 기반으로 정리 노트 만들어줘"* → 검색 → 작성 → `write_file` → 자동 인제스트

### 개념 그래프

- *"rag 자료에서 개념 그래프를 20청크씩 만들어줘"* → `graph_list_chunks(category="rag", limit=20)` 반복 + `graph_store_concepts`
- *"개념 노트를 볼트에 반영해줘"* → `sync_concept_notes()`
- *"DI, IoC, Bean, Container가 어떻게 연결돼 있어?"* → `data/_concepts/`의 해당 개념 노트를 직접 읽어 관계·출처 확인

## 동작 흐름

```
Claude Code 대화
    ↓
Claude Code가 사용자 의도와 필요한 근거 판단
    ↓
PKB MCP 도구 호출
    ├─ search_knowledge      → ES 하이브리드 검색
    ├─ write_file            → data/ 하위 .md 작성 + 자동 인제스트
    ├─ add_document          → 청킹 → 임베딩 → ES 인덱싱
    ├─ convert_and_ingest    → md 변환(PDF는 pdfminer 페이지 보존, 그 외 markitdown) → data/ 저장 → ES 인덱싱
    ├─ sync_corpus / sync_obsidian → 원본 재조정 (업서트 + 유령 문서 정리)
    ├─ get_document          → 문서별 청크/section_path 조회
    └─ graph_*               → SQLite 개념 그래프 조회/저장
    ↓
도구 결과를 Claude Code 대화 컨텍스트에 반영
```

MCP에서는 Claude Code 자체가 도구 선택, 재검색, 요약, 파일 작성을 담당합니다.

## 주의사항

- 데이터 코퍼스의 실제 위치는 `DATA_ROOT`(기본: 프로젝트 내 `data/`)가 결정합니다. 도구 입력 경로와 doc_id는 위치와 무관하게 항상 `data/...` 형식입니다.
- `write_file`과 `add_document`는 코퍼스(`data/...`) 하위 경로만 허용합니다.
- `write_file`은 `.md`만 허용하고, 기본값으로 저장 후 즉시 인제스트합니다.
- `convert_and_ingest`는 원본 파일 위치에 제한이 없지만, 변환 결과는 항상 코퍼스의 `<category>/`에 저장됩니다.
- `sync_corpus`가 기본 재조정 도구입니다(`data/` 코퍼스 전체 업서트 + 유령 문서 정리). `sync_obsidian`은 `DATA_ROOT` 밖에 남겨둔 볼트 파일이 있을 때만 쓰는 선택 경로로, 외부 Obsidian 볼트를 읽어 ES에만 저장합니다(원본은 복사·수정하지 않음). `DATA_ROOT`가 볼트 안에 있으면 그 서브트리는 크롤에서 제외됩니다(이중 인제스트 방지). 둘 다 원본에서 사라진 문서는 정리(prune)하되, 21개 이상 대량 삭제는 `confirm_prune=True`를 요구합니다.
- 대규모 Graph RAG 구축은 시간이 들 수 있으므로 특정 카테고리(예: `rag`) 또는 단일 `doc_id`부터 진행합니다.
