# Claude Code MCP 연동 (기본 사용 방법)

PKB를 MCP 서버로 실행하면 Claude Code 어느 세션에서든 개인 지식 베이스를 검색하고 파일을 작성할 수 있습니다. **이것이 PKB의 기본 사용 방식**입니다.

Claude Code 자체가 에이전트 역할을 하므로 별도의 CLI 에이전트(`pkb chat`)를 실행할 필요가 없습니다.

## 사전 요구사항

- Elasticsearch가 실행 중: `docker compose up -d`
- Python 의존성 설치: `uv sync`
- 인덱스 초기화: `uv run pkb init`

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

절대경로를 실제 경로로 교체.

## 확인

Claude Code 재시작 후 `/mcp` 커맨드로 `pkb` 서버가 연결됐는지 확인.

## 제공되는 도구

| 도구 | 역할 |
|------|------|
| `search_knowledge` | 하이브리드 검색 (BM25 + kNN + RRF + CrossEncoder 재순위). 카테고리 필터 가능 |
| `write_file` | 파일 작성 (`data/` 하위의 `.md`만 허용). 작성 후 자동 인제스트 |
| `list_documents` | 저장된 문서 목록 (카테고리별 필터 가능) |
| `add_document` | `data/` 하위 파일 인제스트 |
| `get_document` | 특정 문서의 모든 청크 + section_path 조회 |
| `reindex_document` | 원본 파일로부터 재인제스트 (수정 후 동기화) |
| `doctor` | ES 연결/인덱스 상태/설정 점검 |
| `convert_and_ingest` | 외부 파일(PDF/DOCX/...) 변환 → `data/`에 `.md` 저장 + 인제스트. Claude Code가 내용을 보고 category를 직접 지정 (내부 API 호출 없음) |
| `sync_obsidian` | Obsidian 볼트 일괄 인제스트 (category=obsidian, doc_id 접두사 obsidian/) |

## 사용 예시

Claude Code에서 자연스럽게 대화하면 적절한 도구가 자동 호출됩니다:

### 검색

- *"내 경력에서 Python 관련 프로젝트 찾아줘"* → `search_knowledge(category="career")`
- *"study 자료 중 벡터 검색 관련 내용 있어?"* → `search_knowledge(category="study")`

### 외부 파일 추가

- *"이 PDF 논문 인제스트해줘: `~/Downloads/paper.pdf`"* → `convert_and_ingest` (카테고리 자동 분류)
- *"`~/Documents/notes.docx`를 study 카테고리로 변환해서 넣어줘"* → `convert_and_ingest(category="study")`
- *"`/Users/jk/Documents/Obsidian Vault` 전체를 동기화해줘"* → `sync_obsidian(path="/Users/jk/Documents/Obsidian Vault")`

### 문서 관리

- *"저장된 study 문서 목록 보여줘"* → `list_documents(category="study")`
- *"전체 문서 몇 개나 있어?"* → `list_documents()`

### 파일 생성

- *"방금 검색한 내용을 요약해서 `data/writing/summary.md`에 저장해줘"* → `search_knowledge` + `write_file`
- *"찾은 내용 기반으로 정리 노트 만들어줘"* → 검색 → 작성 → `write_file`

## 동작 흐름

```
Claude Code 대화
    ↓
Claude가 사용자 의도 파악
    ↓
PKB MCP 도구 호출 결정
    ↓
[도구 실행] MCP 서버 (mcp_server.py)
    ├─ search_knowledge → Elasticsearch 하이브리드 검색
    ├─ write_file       → data/ 하위에 .md 작성
    ├─ list_documents   → ES 집계 쿼리
    ├─ add_document     → 청킹 → 임베딩 → ES 인덱싱
    └─ convert_and_ingest → markitdown 변환 → data/ 저장 → ES 인덱싱
    ↓
도구 결과를 Claude 대화에 반영
```

## 주의사항

- MCP 서버는 프로젝트 디렉터리(`data/`가 있는 곳)를 기준으로 작업합니다. `write_file`과 `add_document`는 해당 `data/` 하위로만 제한됩니다 (경로 순회 방어).
- `convert_and_ingest`는 원본 파일 위치에 제한이 없으나, 변환 결과는 항상 `data/<category>/`에 저장됩니다.
- `sync_obsidian`은 외부 Obsidian 볼트를 읽어 ES에만 저장합니다(`data/`에 파일 복사 없음). 원본은 그대로 보존됩니다.
- **실시간 감시**는 MCP 도구로 불가능합니다 (장기 실행 프로세스). 필요 시 별도 터미널에서 `uv run pkb watch`를 실행하세요.
