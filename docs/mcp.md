# Claude Code MCP 연동 (기본 사용 방법)

PKB의 기본 인터페이스는 **Claude Code + MCP**입니다. Claude Code가 에이전트 역할을 하고, PKB는 검색/파일 작성/인제스트/그래프 조회 도구를 MCP로 제공합니다.

CLI와 Web UI는 운영·검증·대체 인터페이스입니다. 평소 사용은 별도의 `pkb chat` 없이 Claude Code 대화에서 처리합니다.

## 사전 요구사항

- Elasticsearch 실행: `docker compose up -d`
- Python 의존성 설치: `uv sync`
- 인덱스 초기화: `uv run pkb init`

MCP만 사용할 때는 `.env`의 `ANTHROPIC_API_KEY`가 필수는 아닙니다. Claude Code가 LLM 역할을 하기 때문입니다. 단, `pkb graph build` 같은 CLI 일괄 그래프 빌드는 별도 API 키가 필요합니다.

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
| `search_knowledge` | 개인 지식 베이스 검색. BM25와 kNN을 분리 검색한 뒤 RRF로 결합하고 CrossEncoder로 재순위 |
| `write_file` | `data/` 하위 `.md` 작성. 기본값으로 작성 직후 자동 인제스트 |
| `list_documents` | ES에 저장된 문서 목록 조회 |
| `add_document` | `data/` 하위 파일 또는 디렉터리 인제스트 |
| `convert_and_ingest` | 외부 PDF/DOCX/PPTX/XLSX/HTML을 `.md`로 변환해 `data/<category>/`에 저장하고 인제스트 |
| `sync_obsidian` | Obsidian 볼트를 `category=obsidian`, `doc_id=obsidian/...` 형태로 일괄 인제스트 |
| `get_document` | 특정 문서의 모든 청크와 `section_path` 조회 |
| `reindex_document` | 특정 원본 문서를 다시 읽어 ES에 재인제스트 |
| `doctor` | ES 연결, 인덱스, 청크 수, 주요 설정 점검 |

### Graph RAG

| 도구 | 역할 |
|------|------|
| `graph_list_chunks` | 개념 추출 대상 청크를 페이지 단위로 조회 |
| `graph_store_concepts` | Claude Code가 추출한 개념/관계 JSON을 SQLite 그래프 DB에 저장 |
| `search_concepts` | 개념 그래프에서 유사 개념 검색 |
| `explain_concept` | 개념 설명, 직접 관계, 언급 문서 조회 |
| `related_concepts` | 특정 개념의 이웃 개념 조회 |

Graph RAG의 MCP-first 흐름은 `graph_list_chunks`로 청크를 읽고, Claude Code가 직접 개념과 관계를 추출한 뒤, `graph_store_concepts`로 저장하는 방식입니다. 별도 빌드 에이전트를 필수로 두지 않습니다.

## 사용 예시

Claude Code에서 자연스럽게 대화하면 적절한 MCP 도구가 호출됩니다.

### 검색

- *"내 경력에서 Python 관련 프로젝트 찾아줘"* → `search_knowledge(category="career")`
- *"study 자료 중 벡터 검색 관련 내용 있어?"* → `search_knowledge(category="study")`
- *"DI IoC 의존성 주입 관련 노트 찾아줘"* → `search_knowledge(category="obsidian")`

### 외부 파일 추가

- *"이 PDF 논문을 study 카테고리로 넣어줘: `~/Downloads/paper.pdf`"* → `convert_and_ingest(category="study")`
- *"`~/Documents/notes.docx`를 writing 카테고리로 변환해서 넣어줘"* → `convert_and_ingest(category="writing")`
- *"`/Users/jk/Documents/Obsidian Vault` 전체를 동기화해줘"* → `sync_obsidian(path="/Users/jk/Documents/Obsidian Vault")`

`convert_and_ingest`의 `category`는 필수입니다. Claude Code가 파일명/사용자 지시/내용 맥락을 보고 `about`, `career`, `study`, `writing`, `misc` 중 하나를 지정합니다.

### 문서 관리

- *"저장된 study 문서 목록 보여줘"* → `list_documents(category="study")`
- *"이 문서 전체 청크와 섹션 경로 보여줘: `data/study/rag/xxx.md`"* → `get_document(doc_id="data/study/rag/xxx.md")`
- *"방금 수정한 Obsidian 문서 다시 인덱싱해줘"* → `reindex_document(doc_id="obsidian/...")`

### 파일 생성

- *"방금 검색한 내용을 요약해서 `data/writing/summary.md`에 저장해줘"* → `search_knowledge` + `write_file`
- *"찾은 내용 기반으로 정리 노트 만들어줘"* → 검색 → 작성 → `write_file` → 자동 인제스트

### 개념 그래프

- *"study RAG 자료에서 개념 그래프를 20청크씩 만들어줘"* → `graph_list_chunks(category="study", limit=20)` 반복 + `graph_store_concepts`
- *"DI, IoC, Bean, Container가 어떻게 연결돼 있어?"* → `search_concepts` + `explain_concept` + `related_concepts`
- *"RRF와 리랭커 주변 개념을 보여줘"* → `search_concepts("RRF 리랭커")` + `related_concepts`

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
    ├─ convert_and_ingest    → markitdown 변환 → data/ 저장 → ES 인덱싱
    ├─ sync_obsidian         → Obsidian 볼트 일괄 인제스트
    ├─ get_document          → 문서별 청크/section_path 조회
    └─ graph_*               → SQLite 개념 그래프 조회/저장
    ↓
도구 결과를 Claude Code 대화 컨텍스트에 반영
```

CLI/Web의 LangGraph 에이전트는 이 흐름에 끼지 않습니다. MCP에서는 Claude Code 자체가 도구 선택, 재검색, 요약, 파일 작성을 담당합니다.

## 주의사항

- MCP 서버는 프로젝트 디렉터리(`data/`가 있는 곳)를 기준으로 작업합니다.
- `write_file`과 `add_document`는 `data/` 하위 경로만 허용합니다.
- `write_file`은 `.md`만 허용하고, 기본값으로 저장 후 즉시 인제스트합니다.
- `convert_and_ingest`는 원본 파일 위치에 제한이 없지만, 변환 결과는 항상 `data/<category>/`에 저장됩니다.
- `sync_obsidian`은 외부 Obsidian 볼트를 읽어 ES에만 저장합니다. 원본 파일은 복사하거나 수정하지 않습니다.
- 실시간 감시는 MCP 도구로 실행하지 않습니다. 필요 시 별도 터미널에서 `uv run pkb watch`를 실행합니다.
- 대규모 Graph RAG 구축은 비용과 시간이 들 수 있으므로 `study` 또는 단일 `doc_id`부터 진행합니다.
