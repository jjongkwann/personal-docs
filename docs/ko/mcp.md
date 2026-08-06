# MCP 연동 (기본 사용 방법)

PKB의 기본 인터페이스는 **에이전트 + MCP**입니다. Claude Code·Codex·Gemini CLI가 에이전트 역할을 하고, PKB는 검색/파일 작성/인제스트/그래프 조회 도구를 MCP로 제공합니다.

CLI는 색인·검증·디버깅용 보조 인터페이스입니다. 평소 사용은 에이전트 대화에서 처리합니다. 이 문서에서
**에이전트/CLI 세션**은 Claude Code·Codex·Gemini의 대화/프로세스를 뜻하며 MCP 프로토콜 세션과는 다릅니다.

## 왜 stdio가 아니라 HTTP 공유 서버인가

> **stdio로 등록하지 마세요.** MCP stdio는 **에이전트/CLI 세션마다 서버 프로세스를 새로 띄웁니다.** PKB 서버는
> 임베딩 모델과 CrossEncoder 리랭커를 올리므로 프로세스 한 개가 약 **4GB**를 씁니다. 에이전트/CLI 세션을 여러 개
> 열거나 에이전트를 팬아웃하면 에이전트/CLI 세션 수 × 4GB가 되어 머신이 스왑으로 죽습니다.
>
> 그래서 PKB는 **127.0.0.1에 뜬 단일 HTTP 서버**를 모든 클라이언트가 공유합니다. 모델은 한 벌만
> 상주하고, 에이전트/CLI 세션을 몇 개 열든 메모리는 그대로입니다.

Streamable HTTP는 여러 클라이언트가 하나의 서버 프로세스를 공유하도록 설계된 전송 방식이라, 이 구조가 명세에도 부합합니다.

## 무상태 Streamable HTTP (MCP 2026-07-28)

2026-07-28 MCP 개정에서는 프로토콜 세션을 제거했습니다. `initialize` 핸드셰이크와
`Mcp-Session-Id`가 없으며, 각 Streamable HTTP 요청은 독립적으로 처리되고 요청별 메타데이터를
전달합니다. 클라이언트가 `Mcp-Method`와 `Mcp-Name` 헤더를 자동 처리하므로 사용자가 헤더를 직접
추가하거나 관리할 필요가 없습니다. 아래 endpoint와 클라이언트 등록 명령은 그대로 사용합니다.

무상태 전송이라고 해서 요청마다 PKB 프로세스를 새로 띄우는 것은 아닙니다. 모든 클라이언트가
localhost의 단일 HTTP 프로세스(메모리 모델 한 벌)를 계속 공유합니다. 호출 간 애플리케이션 상태는
Elasticsearch/SQLite에 저장하거나 tool 인자/handle로 명시적으로 전달하며, 프로토콜 세션에 저장하지 않습니다.

## 사전 요구사항

- Elasticsearch 실행: `docker compose up -d`
- Python 의존성 설치: `uv sync`
- 인덱스 초기화: `uv run pkb init`

API 키가 필요 없습니다 — 에이전트가 LLM 역할을 하는 100% 로컬 구성입니다.

## 1. 서버 상시 기동 (macOS / launchd)

`~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.jongkwan.pkb-mcp</string>
    <key>ProgramArguments</key>
    <array>
        <string>/ABSOLUTE/PATH/TO/uv</string>
        <string>--directory</string>
        <string>/ABSOLUTE/PATH/TO/personal-docs</string>
        <string>run</string>
        <string>python</string>
        <string>-m</string>
        <string>pkb.mcp_server</string>
    </array>
    <!-- config.py의 env_file=".env"가 상대경로라 cwd가 레포 루트여야 한다 -->
    <key>WorkingDirectory</key>
    <string>/ABSOLUTE/PATH/TO/personal-docs</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/pkb-mcp.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/pkb-mcp.err.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist
```

로그인할 때 자동으로 뜨고, 죽으면 `KeepAlive`가 되살립니다. `RunAtLoad`+`KeepAlive` 조합이라
**`pkill`로는 멈추지 않습니다** — 멈추려면 `launchctl unload`를 쓰세요.

서버를 직접 띄워도 됩니다(개발·디버깅용). 단 **두 개를 동시에 띄우지 마세요** — 포트 충돌로
launchd가 무한 재기동 루프에 빠지고, 실패한 기동마다 모델을 올리려다 메모리를 갉습니다.

```bash
uv run python -m pkb.mcp_server   # 포그라운드
```

## 2. 클라이언트 등록

세 클라이언트 모두 streamable HTTP를 지원합니다. **인자 문법이 서로 다릅니다.** 아래 명령은 무상태
전환 후에도 같은 endpoint를 사용하며, 각 클라이언트가 프로토콜 메타데이터와 헤더를 자동 처리합니다.

```bash
# Claude Code
claude mcp add --transport http pkb http://127.0.0.1:8787/mcp -s user

# Codex  (--url 플래그)
codex mcp add pkb --url http://127.0.0.1:8787/mcp

# Gemini CLI  (-t http, 그리고 -s user 필수 — 기본 스코프가 project라 빼면 그 폴더에서만 잡힘)
gemini mcp add pkb http://127.0.0.1:8787/mcp -t http -s user
```

Gemini는 **신뢰하지 않는 폴더에서 MCP를 억제합니다** — user 스코프 서버까지 막힙니다.
`~/.gemini/trustedFolders.json`에 작업 폴더가 없으면 `Disabled`로 뜹니다.

### Claude Desktop (브리지 필요)

Claude Desktop만 HTTP를 직접 못 붙습니다. `claude_desktop_config.json`의 `mcpServers`는 **stdio 전용**이고,
설정 > 커넥터의 커스텀 커넥터는 Anthropic 클라우드에서 서버로 접속하는 구조라 localhost에 도달하지 못합니다
(평문 `http://`는 localhost라도 거부).

> ⚠️ `claude_desktop_config.json`에 `"url"` 필드를 넣지 마세요. Desktop이 `mcpServers` 섹션 전체를
> **에러 없이 조용히 날려버립니다.**

그래서 stdio↔HTTP 브리지(`mcp-remote`)를 끼웁니다. `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pkb": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8787/mcp", "--allow-http"]
    }
  }
}
```

브리지는 가벼운 Node 프로세스라 모델을 들지 않습니다 — 무거운 4GB는 공유 HTTP 서버 한 벌뿐입니다.
설정 후 Desktop을 재시작하세요.

포트를 바꾸려면 `.env`의 `MCP_PORT`를 고치고 위 URL들도 같이 바꾸세요.

## 3. 확인

```bash
uv run pkb doctor
```

`=== MCP 서버 ===` 절에 LISTEN 여부·PID·가동시간·메모리·device·launchd 누적 기동 횟수가 나옵니다.
누적 기동이 1회를 넘으면 재기동이 반복됐다는 뜻이니 `pkb-mcp.err.log`를 확인하세요.

각 클라이언트에서는 `claude` → `/mcp`, `codex mcp list`, `gemini mcp list`로 연결을 확인합니다.

## 제공 도구

### 검색/문서 작업

| 도구 | 역할 |
|------|------|
| `search_knowledge` | 개인 지식 베이스 하이브리드 검색. BM25와 kNN을 msearch 한 요청으로 검색한 뒤 RRF로 결합(CrossEncoder 재순위는 기본 비활성), 문서당 최대 2청크로 캡. 개념 그래프가 있으면 히트별 관련 개념 링크와 재검색용 개념 어휘를 함께 부착. `include_obsidian=False`로 코퍼스 밖(볼트 직속, `obsidian/` 접두 doc_id) 문서 제외 가능. 기본 검색이 부실하면 `query_variants`(RAG-Fusion 쿼리 변형, 최대 3개)로 변형 쿼리를 함께 검색해 RRF 병합 |
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
| `graph_explain` | 단일 개념의 별칭·큐레이션·양방향 관계·언급·confidence·출처 청크 evidence 조회 |
| `graph_path` | 두 개념 사이의 제한된 최단 경로 조회. 방향 순회와 relation 필터 지원 |
| `graph_query` | 로컬 개념 임베딩으로 의미/어휘 시드를 고른 뒤 제한된 SQLite 관계 하위 그래프 반환 |
| `graph_affected` | 한 개념에서 저장된 `src→dst` 방향의 하위 개념 조회. relation 타입 필터 지원 |
| `graph_list_chunks` | 개념 추출 대상 청크를 페이지 단위로 조회. `pending_only=True`면 미추출·내용 변경 청크만 반환(증분 추출) |
| `graph_store_concepts` | Claude Code가 추출한 개념/관계 JSON을 SQLite 그래프 DB에 저장 |
| `graph_curate` | 개념 큐레이션 — 미큐레이션 목록 조회, real/vocab 라벨·산문 저장 |
| `graph_merge` | 표기 변형 개념을 대표 개념으로 병합 (엣지/멘션/별칭 승계) |
| `sync_concept_notes` | SQLite 개념그래프를 `data/_concepts/<slug>.md` 볼트 노트로 투영 + `_concepts/index.md` MOC(개념 어휘 카탈로그 진입점) 렌더 |

Graph RAG의 MCP-first 흐름은 `graph_list_chunks`로 청크를 읽고, Claude Code가 직접 개념과 관계를 추출한 뒤, `graph_store_concepts`로 저장하는 방식입니다. 저장 후 `graph_curate`로 real/vocab을 라벨링하고 표기 변형은 `graph_merge`로 병합한 뒤 `sync_concept_notes`로 투영합니다. 관계 조회는 `graph_explain`/`graph_path`/`graph_query`/`graph_affected`로 SQLite를 직접 읽고, 투영 노트는 사람이 읽는 Obsidian 뷰로 사용합니다.

### 파라미터 레퍼런스

`src/pkb/mcp_server.py`에 등록된 그대로의 시그니처:

```python
search_knowledge(query, category="", top_k=5, include_archived=False,
                 include_obsidian=True, query_variants=[], profile="all",
                 canonical_group=True, canonical_boost=0.15)
write_file(file_path, content, ingest=False, dry_run=False,
           expected_hash="", strict_policy=True)
list_documents(category="", include_archived=False, limit=50)
add_document(file_path, tags="")
convert_and_ingest(input_path, category, output_name="", ingest=True)
get_document(doc_id, include_content=False, chunk_range="")
reindex_document(doc_id)
sync_corpus(confirm_prune=False)
sync_obsidian(path="", confirm_prune=False)
archive_document(doc_id, reason="")
restore_document(doc_id)
doctor()

graph_list_concepts(category="", limit=500)
graph_explain(concept, edge_limit=30, evidence_limit=5, mention_limit=20)
graph_path(source, target, max_hops=4, directed=False, relations=[], evidence_limit=3)
graph_query(query, depth=2, seed_limit=3, max_nodes=30, min_similarity=0.4,
            relations=[], evidence_limit=3)
graph_affected(concept, max_depth=2, max_nodes=30, relations=[], evidence_limit=3)
graph_list_chunks(category="", doc_id="", offset=0, limit=20, pending_only=False)
graph_store_concepts(items_json)
graph_curate(items_json="")
graph_merge(winner_slug, loser_slugs_json)
sync_concept_notes(confirm_prune=False)
```

`add_document(tags=...)`는 쉼표 구분 문자열을 받고, 그래프 도구의 `relations`는 **리스트**를
받습니다. CLI 쪽은 의도적으로 갈라져 있습니다 — `pkb graph …`는 쉼표 구분 문자열을 받는
`--relation`을 씁니다(`cli.py:_graph_relations`). 셸에서는 플래그를 반복하는 것보다 문자열 하나가
다루기 쉽기 때문입니다.

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

- *"정본만 찾아줘"* → `search_knowledge(profile="curated")`
- *"연구 근거까지 찾아줘"* → `search_knowledge(profile="evidence")`
- *"찾은 내용 기반으로 정리 노트 만들어줘"* → 검색 → 경로·canonical_id 결정 →
  `write_file(dry_run=True)` → diff 확인 → `expected_hash`로 적용 → 배치 끝에 동기화

### 개념 그래프

- *"rag 자료에서 개념 그래프를 20청크씩 만들어줘"* → `graph_list_chunks(category="rag", limit=20)` 반복 + `graph_store_concepts`
- *"개념 노트를 볼트에 반영해줘"* → `sync_concept_notes()`
- *"Dependency Injection을 근거와 함께 설명해줘"* → `graph_explain(concept="Dependency Injection")`
- *"DI에서 Container까지 최단 관계 경로를 보여줘"* → `graph_path(source="DI", target="Container")`
- *"DI, IoC, Bean, Container가 어떻게 연결돼 있어?"* → `graph_query(query="DI, IoC, Bean, Container의 관계")`
- *"DI를 선수 개념으로 두는 후속 개념은?"* → `graph_affected(concept="DI", relations=["prerequisite_of"])`

## 동작 흐름

```
Claude Code 대화
    ↓
Claude Code가 사용자 의도와 필요한 근거 판단
    ↓
PKB MCP 도구 호출
    ├─ search_knowledge      → ES 하이브리드 검색
    ├─ write_file            → 정책 검사·diff/hash 미리보기 → data/ 하위 .md 작성
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
- `write_file`은 `.md`만 허용하며 MCP 기본은 `ingest=False`입니다. 기존 파일은 먼저
  `dry_run=True`로 diff와 `previous_hash`를 받고, 적용 호출에 `expected_hash`를 넣어야 합니다.
- `concepts/`, `guides/`, `research/`, `00_MOC.md` 쓰기는 문서 계약 frontmatter를 강제합니다.
- `convert_and_ingest`는 원본 파일 위치에 제한이 없지만, 변환 결과는 항상 코퍼스의 `<category>/`에 저장됩니다.
- `sync_corpus`가 기본 재조정 도구입니다(`data/` 코퍼스 전체 업서트 + 유령 문서 정리). `sync_obsidian`은 `DATA_ROOT` 밖에 남겨둔 볼트 파일이 있을 때만 쓰는 선택 경로로, 외부 Obsidian 볼트를 읽어 ES에만 저장합니다(원본은 복사·수정하지 않음). `DATA_ROOT`가 볼트 안에 있으면 그 서브트리는 크롤에서 제외됩니다(이중 인제스트 방지). 둘 다 원본에서 사라진 문서는 정리(prune)하되, 21개 이상 대량 삭제는 `confirm_prune=True`를 요구합니다.
- 대규모 Graph RAG 구축은 시간이 들 수 있으므로 특정 카테고리(예: `rag`) 또는 단일 `doc_id`부터 진행합니다.
