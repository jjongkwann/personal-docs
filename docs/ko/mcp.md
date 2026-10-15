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
    <!-- launchd 기본 fd 소프트 상한은 256이다. torch + MPS만으로도 dylib과 Metal 셰이더
         캐시로 220개 가까이 상주 점유하므로, 장시간 가동한 서버는 EMFILE에 걸려
         코퍼스 파일과 그래프 SQLite를 열지 못한다. -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>4096</integer>
    </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist
```

`NumberOfFiles`를 빼면 fd 고갈이 서로 무관해 보이는 두 오류로 나타납니다. `write_file`은
존재하는 파일에 대해 `기존 파일을 읽을 수 없습니다: <경로>`를 반환하고,
`search_knowledge`·`doctor`는 `OperationalError: unable to open database file`을 반환합니다.
권한 문제를 의심하기 전에 열린 fd 수를 먼저 세십시오.
`for PID in $(pgrep -f pkb.mcp_server); do lsof -p $PID | wc -l; done`

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

### 다른 머신에서 돌리기 (LAN / VPN)

서버를 항상 켜져 있는 머신에 두고 노트북에서 붙이려면 바인딩 주소를 엽니다. 서버 쪽 `.env`에
`MCP_HOST=0.0.0.0`을 넣거나, launchd plist의 `EnvironmentVariables`에 같은 값을 넣습니다
(환경변수가 `.env`보다 우선하므로 `.env`를 건드리지 않아도 됩니다). 클라이언트는 URL의 호스트만
서버 IP로 바꿉니다.

```bash
claude mcp add --transport http pkb http://192.168.0.5:8787/mcp -s user
```

Elasticsearch는 `network.host: 127.0.0.1`로 두세요. MCP 서버만 열면 됩니다.

> ⚠️ MCP 서버에는 인증이 없고 `write_file` 툴이 볼트 어디에나 쓸 수 있습니다. 홈 LAN이나 본인이
> 운영하는 VPN 안에서만 여세요. 이 포트를 공유기에서 인터넷으로 포트포워딩하면 안 됩니다.

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
| `read_file` | `data/**.md` 파일의 디스크 원문과 `content_hash` 반환 (`get_document`는 ES 청크를 돌려주므로 원문으로 되돌릴 수 없음) |
| `patch_file` | 기존 문서에서 `old`와 정확히 일치하는 한 곳을 `new`로 교체 — 전체 파일을 다시 보내지 않는 부분 편집. `write_file`과 같은 정책 검사와 낙관적 잠금(`expected_hash`) 적용 |
| `list_documents` | ES에 저장된 문서 목록. `date_modified` 내림차순으로 상위 `limit`(기본 50)개만 표시. `limit<=0`이면 전체. 문서 단위 색인이 있으면 그 행을 읽어 `category` 없이 불러도 시간 초과가 나지 않음 |
| `list_categories` | 문서 단위 색인 집계 한 번으로 `{"documents": N, "categories": {이름: 문서 수}}` 반환. 카테고리 목록만 필요하면 `list_documents`·`doctor` 대신 사용 |
| `related_documents` | 문서 벡터(청크 임베딩 평균)가 주어진 `doc_id`와 가까운 다른 문서 반환(자기 자신 제외). `search_knowledge`와 같은 범위 필터 사용 |
| `list_files` | `data/` 폴더의 파일·하위 폴더 목록. 색인되지 않은 `_origin` 첨부파일도 포함하며 `next_offset`으로 페이지 조회 |
| `get_file` | `data/` 원본 파일을 MCP embedded resource의 base64 blob으로 전송. 파일명·MIME·크기·SHA-256 포함, 파일당 최대 10 MiB |
| `put_attachment` | 볼트 밖 첨부 폴더(`ATTACHMENTS_ROOT`, 예: 회의 오디오)에 최대 4 MiB 조각으로 업로드. 마지막 조각의 SHA-256이 맞아야 파일이 생기고, 이미 있는 파일은 덮어쓰지 않음. 색인하지 않음 |
| `get_attachment` | `ATTACHMENTS_ROOT` 아래 파일을 최대 4 MiB 조각으로 내려받음. `data_base64`를 담은 JSON 텍스트, 마지막 조각에 파일 SHA-256 |
| `search_media` | 글로 이미지·오디오를 찾음 (미디어 벡터만 kNN). 오디오는 30초 구간 단위로 `start`/`end`와 함께 나오며, 한 파일의 여러 구간이 순위에 들 수 있음. [이미지·오디오 검색](#이미지오디오-검색) 참고 |
| `get_media` | `list_media`·`search_media`가 돌려준 이미지·오디오를 최대 4 MiB 조각으로 내려받음. `get_attachment`와 같은 JSON에 `doc_id`, `kind`, `mime_type`이 더해짐 |
| `list_media` | `data/`, 볼트, `ATTACHMENTS_ROOT`의 이미지·오디오와 크기·수정 시각·SHA-256, 지금 벡터가 있는지. `pkb media-sync`가 씀 |
| `put_media_embeddings` | 클라이언트가 만든 벡터로 파일 하나의 미디어 청크를 통째로 바꿈. 모델·파일 해시·차원·벡터 공간이 맞지 않으면 거부 |
| `add_document` | `data/` 하위 파일 인제스트 (md/txt/pdf/docx/pptx/xlsx/html) |
| `convert_and_ingest` | 외부 PDF/DOCX/PPTX/XLSX/HTML을 `.md`로 변환해 `data/<category>/`에 저장하고 인제스트. 변환본 상단에 provenance frontmatter(`source`·`converted_from`·`converted_at`) 기록, PDF는 페이지 마커(`## p.N`) 보존 |
| `sync_corpus` | `data/` 코퍼스를 ES와 재조정: 업서트 + 코퍼스에 없는 문서 정리 (`confirm_prune=True`로 대량 삭제 승인). Syncthing 충돌 사본(`*.sync-conflict-*`)은 색인하지 않고, 색인에 남아 있으면 확인 없이 지움 |
| `sync_obsidian` | Obsidian 볼트를 ES와 재조정: 업서트 + 볼트에 없는 문서 정리. 카테고리는 볼트 첫 폴더명에서 동적으로 파생(`data/`와 동일 규칙). OBSIDIAN_PATH 미설정 시 잔존 `obsidian/*` 전체 정리 제안 (`confirm_prune=True` 필요) |
| `get_document` | 문서 조회. 기본(`include_content=False`)은 메타(제목·카테고리·수정일) + 청크 목차만 반환. `chunk_range`(예: `"3-7"`)로 해당 구간만 전문, `include_content=True`면 전체 전문 |
| `reindex_document` | 특정 원본 문서를 다시 읽어 ES에 재인제스트 |
| `archive_document` | 문서를 soft delete(검색 제외, 복구 가능)로 아카이브. `data/` 하위 `.md` 원본은 frontmatter에 `archived_at` 기록 후 재인제스트(파일이 SSOT), 그 외는 ES에만 기록 |
| `restore_document` | 아카이브된 문서를 검색 대상으로 복구. frontmatter에 아카이브 기록이 있으면 제거 후 재인제스트 |
| `doctor` | ES 연결, 인덱스, 청크 수, 설정, 개념 그래프 통계 점검 + 조치 후보(만료 미아카이브·purge 대상·고아 개념) 나열 |

`search_knowledge(expand_context=N)`은 전후 N개 청크를 붙입니다(0~4, 생략 시
`EXPAND_CONTEXT`). `max_context_tokens`(256~32000, 기본 4000)는 출처 표기·개념 어휘·본문·
주변 문맥을 합친 출력 예산입니다. `top_k`나 확장 범위를 늘려도 이 예산을 넘기지 않습니다.

### 항목 메타데이터, 범위 필터, 문서 단위 색인

인제스트는 다음 frontmatter 키를 모든 청크에 색인합니다. 키가 없는 노트는 그대로입니다.

| 키 | 뜻 |
|---|---|
| `source_type` | `meeting`, `event`, `email`, `drive`, `note` (소문자로 저장) |
| `occurred_at` | 항목이 일어난 날짜·시각 (ISO 8601). 해석할 수 없으면 버리고 쓰기 경고를 냄 |
| `people` | 참석자, 보낸·받은 사람, 소유자. 이메일은 소문자로, 이름은 그대로 |
| `source_id`, `source_link` | 원본 서비스의 ID와 URL. `source_link`는 저장만 하고 검색하지 않음 |
| `links` | 이어진 항목의 doc_id |
| `attachments` | `ATTACHMENTS_ROOT` 기준 상대 경로 |

`search_knowledge`와 `related_documents`는 같은 범위 필터를 받고, BM25와 kNN 양쪽에
적용합니다: `doc_ids`(이 문서들 안에서만, 예: 회의에 연결된 항목), `source_types`, `people`,
`date_from`/`date_to`(`occurred_at` 기준 양 끝 포함. 시각 없이 날짜만 주면 UTC 하루 전체이며,
`occurred_at`이 없는 문서는 빠짐), `path_prefix`(doc_id 접두사). 빈 목록은 제한 없음입니다.

문서 단위 색인(`<ES_INDEX>_docs`)은 문서마다 한 행으로 메타데이터, 청크 수, `doc_vector`(청크
임베딩 평균의 L2 정규화)를 둡니다. 한 번 생기면 인제스트·삭제·아카이브·복구·purge가 함께
고칩니다. 저장된 청크 벡터로 만들고 채우려면(재임베딩 없음) `uv run pkb doc-index`를 실행합니다.
그 전에는 `list_documents`가 청크 집계로 돌아가고, `list_categories`·`related_documents`는
실행을 안내하는 오류를 냅니다.

### 볼트 밖 첨부

`ATTACHMENTS_ROOT`(예: `/Volumes/PKBData/attachments`)를 설정하면 `put_attachment`와
`get_attachment`가 켜집니다. 경로는 그 폴더 기준 상대 경로이며, 절대경로·`..`·숨김 성분·
폴더 밖으로 풀리는 경로는 거부합니다.

올리기: 조각을 차례로 보내고 `offset`은 서버가 지금까지 받은 바이트 수(앞 응답의 `received`)와
같아야 합니다. `offset=0`은 처음부터 다시 받습니다. 마지막 조각에는 전체 파일의 `sha256`이
필요하며, 맞으면 파일을 제자리로 원자적으로 옮기고 다르면 받은 내용을 버립니다. 이미 파일이
있는 경로는 덮어쓰지 않습니다 — 같은 `sha256`으로 다시 보내면 완료로 응답합니다.

내려받기: `eof`가 true가 될 때까지 `get_attachment(path, offset, length)`를 부르고 각
`data_base64`를 디코딩해 이어 붙인 뒤 마지막 응답의 `sha256`으로 검증합니다.

### 이미지·오디오 검색

이미지와 오디오를 글로 찾습니다. 문서와 같은 벡터 공간을 씁니다. 서버는 미디어 모델을 올리지
않습니다. 다른 Mac에서 `pkb media-sync`가 `jinaai/jina-embeddings-v5-omni-small`로 파일을
임베딩해 `put_media_embeddings`로 보냅니다. 이 모델의 텍스트 탑은
`jinaai/jina-embeddings-v5-text-small`에 고정돼 있어서, 서버가 text-small로 만든 질의 벡터로 찾을
수 있습니다 (같은 문장을 두 모델에 넣은 코사인: 한국어·영어, 질의·문서 프롬프트 모두 0.9997 이상,
2026-10 실측).

서버 설정:

```
EMBEDDING_MODEL=jinaai/jina-embeddings-v5-text-small   # 다른 jina v5 설정과 함께, EMBEDDING_TASK=retrieval
MEDIA_EMBEDDING_MODEL=jinaai/jina-embeddings-v5-omni-small
```

`MEDIA_EMBEDDING_MODEL`이 비어 있으면 미디어 색인과 검색이 꺼집니다. 서버는 미디어 모델을 그와
공간이 같은 텍스트 모델·어댑터와 함께일 때만 받습니다 (`src/pkb/media.py`의 `MEDIA_SPACES`).
다른 텍스트 모델(예: bge-m3)이면 `put_media_embeddings`와 `search_media`가 거부하고
`list_media`는 `"model": null`을 돌려줍니다.

**대상.** `data/…`(`DATA_ROOT`), `obsidian/…`(`OBSIDIAN_PATH`에서 `DATA_ROOT` 제외),
`attachments/…`(`ATTACHMENTS_ROOT`). 이미지: png, jpg, jpeg, gif, webp, bmp, tif, tiff, heic, avif,
svg. 오디오: m4a, mp3, wav, aiff, aif, flac, ogg, opus, aac. 제외 규칙은 Markdown 인제스트와
같습니다: 숨김 성분(`.uploads` 포함), 예약 폴더(`_trash`, `_archive`, `_review` 등), Syncthing 충돌
사본, 심볼릭 링크. Markdown 인제스트는 미디어 파일을 읽지 않습니다.

**청크.** 이미지는 하나, 오디오는 30초 이하 창마다 하나입니다 (모델이 오디오를 Whisper 30초 창으로
읽습니다. 클라이언트는 거의 무음인 창을 버립니다). 미디어 청크는 텍스트 청크와 같은 색인에
`source_type` `image`·`audio`로 들어가며 `title`(확장자 뺀 파일 이름), `category`(루트 아래 첫
폴더), `date_modified`(파일 mtime), `media_start`/`media_end`(초, 오디오만), `media_sha256`,
`media_model`, `linked_from`(파일을 가리키는 노트), `content`를 가집니다. `content`는 결과의
`context`로 보이는 짧은 설명입니다: 경로, 연결된 노트마다 제목·`occurred_at`·`people`, 이미지면
`![[이름]]`이나 `![](경로)` 주변 줄. `ATTACHMENTS_ROOT`의 오디오는 노트 frontmatter의
`attachments` 목록으로 연결됩니다. `occurred_at`과 `people`은 연결된 노트에서 오므로 기간·사람
필터가 미디어에도 먹힙니다.

**맥락 갱신.** `sync_corpus`, `sync_obsidian`, `pkb sync`가 색인된 미디어의 노트 맥락을 다시
계산해 바뀐 것만 고칩니다. 다시 임베딩하지 않습니다 — 회의 노트 이름을 바꾸거나 people을 고치면
`linked_from`과 필터가 따라옵니다.

**다른 도구.** `search_knowledge`와 `related_documents`는 `source_types`에 `image`나 `audio`를
넣을 때만 미디어를 냅니다. `list_documents`와 `list_categories`는 미디어를 세지 않습니다
(`list_media`를 쓰세요). 개념 그래프 추출도 미디어 청크를 건너뜁니다.

**정리.** 파일이 남아 있는 미디어는 "원본에 없는 문서"가 되지 않습니다. 파일이 사라지면 평소
목록에 들어갑니다: `sync_corpus`는 `data/`와 `attachments/`(`confirm_prune=True`일 때만 삭제),
`sync_obsidian`은 `obsidian/`, `pkb sync`는 셋 다. `ATTACHMENTS_ROOT`를 설정했는데 폴더가
없으면(볼륨이 빠졌을 때 등) `attachments/`는 정리하지 않고, 설정을 지우면 남은 `attachments/`
항목을 모두 정리 대상으로 보여 줍니다.

응답 (JSON 텍스트):

```
list_media   → {"total": N, "model": "<서버가 받는 미디어 모델>" | null,
                "media": [{"doc_id", "kind": "image"|"audio", "size_bytes", "modified" (ISO 8601 UTC),
                           "sha256", "indexed": bool, "model": "<저장된 벡터의 모델>" | null}]}
get_media    → {"doc_id", "kind", "mime_type", "offset", "length", "total_size", "eof",
                "data_base64", "sha256" (eof일 때만)}
put_media_embeddings → {"doc_id", "kind", "segments": N, "sha256", "model", "linked_from": [doc_id]}
search_media → {"query", "results": [{"doc_id", "kind", "title", "start", "end", "score",
                                      "context", "linked_from": [doc_id]}]}
```

`start`/`end`는 초 단위이며 이미지는 `null`입니다. `score`는 Elasticsearch 코사인 점수
`(1 + cos) / 2`입니다. `put_media_embeddings`는 `segments=[{"start", "end", "vector"}]`를 받습니다:
이미지는 `start`/`end` 없이 하나, 오디오는 겹치지 않는 30초 이하 창(최대 1000개). 벡터는
`EMBEDDING_DIMS`개의 유한한 수이고 서버가 단위 길이로 맞춥니다. `sha256`은 지금 파일과 같아야
합니다.

**클라이언트.** 모델을 돌릴 Mac에서 (`uv sync --extra media`가 클라이언트에만 필요한 torchvision을
더합니다):

```bash
uv run --extra media pkb media-sync --url https://<게이트웨이>/mcp   # Bearer 토큰은 ~/.config/mcp-gateway/token (https만)
uv run --extra media pkb media-sync --url http://<서버>:8787/mcp --dry-run
uv run --extra media pkb media-sync --kinds audio --limit 20
```

벡터가 없거나 낡은 파일을 `list_media(missing_only=True)`로 받아 `get_media`로 내려받고, 문서 쪽
(`retrieval` 어댑터, 가능하면 MPS·bf16, 비전이나 오디오 탑만 적재)으로 임베딩해
`put_media_embeddings`를 부릅니다. 오디오는 macOS `afconvert`로 16 kHz 모노로 풀어 30초 창으로
자르고, SVG·HEIC는 `sips`로 PNG를 만듭니다. 게이트웨이의 `pkb-…` 도구 이름은 `tools/list`에서
찾습니다. 다시 실행하면 이미 최신인 파일은 건너뜁니다.

### 다른 환경에서 원본 파일 받기

`list_files`와 `get_file`은 `core` 프로파일에도 포함됩니다. 검색으로 관련 문서를 찾은 뒤,
문서에 연결된 원본 폴더를 조회하고 필요한 파일을 받습니다. 예를 들어:

```python
list_files(directory="data/about/_origin/personal-brand/ljk")
get_file(file_path="data/about/_origin/personal-brand/ljk/ljk-black-1024.png")
```

`list_files`는 바로 아래 항목만 반환합니다. `next_offset`이 있으면 같은 폴더와 해당
`offset`으로 다음 페이지를 조회합니다(기본 100개, 최대 200개). 숨김 경로·심볼릭 링크·
특수 파일은 제외합니다. 절대경로나 `data/` 밖의 파일은 조회할 수 없습니다.

`get_file`은 별도 HTTP 링크나 서버 로컬 경로를 전달하는 대신 **응답 안에 원본 바이트**를
담습니다. JSON 응답의 `content`에서 `type: resource`인 항목의 `resource.blob`을 base64
디코딩하면 PNG/SVG/PDF 등 원본 파일을 그대로 복원할 수 있습니다. 파일명(`name`),
`mime_type`, `size_bytes`, `sha256`은 `structuredContent`와 짧은 텍스트 블록에 있습니다.
`pkb://files/...` URI는 포함된 파일의 식별자이며 웹 다운로드 주소가 아닙니다.

클라이언트의 첨부파일 표시·저장 UI는 제품마다 다릅니다. 파일 저장 도구가 있는 에이전트는
blob을 디코딩해 저장하고 크기와 SHA-256을 검증하면 됩니다. 원본 10 MiB는 전송 시 약
13.3 MiB의 base64가 되므로 클라이언트·게이트웨이의 응답 크기 제한도 적용됩니다.
이 도구들은 원본이나 검색 인덱스를 변경하지 않습니다.

서버 업데이트 후 PKB 프로세스를 재시작하고, 게이트웨이를 사용한다면 PKB 도구 목록을
갱신해야 합니다. 기존 대화가 도구 목록을 캐시하면 MCP를 재연결하거나 새 대화에서 확인하세요.

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

Graph RAG의 MCP-first 흐름은 `graph_list_chunks`로 청크를 읽고, Claude Code가 직접 개념과 관계를 추출한 뒤, `graph_store_concepts`로 저장하는 방식입니다. 저장 후 `graph_curate`로 real/vocab을 라벨링하고 표기 변형은 `graph_merge`로 병합합니다. 관계 조회는 `graph_explain`/`graph_path`/`graph_query`/`graph_affected`로 SQLite를 직접 읽고, 사람이 보는 지도는 `graph_map`으로 오프라인 HTML을 만듭니다.

`graph_list_chunks`에서 받은 각 청크의 `content_hash`와 `input_hash`를 변경하지 말고
`graph_store_concepts`의 해당 항목에 그대로 넣으세요. `concepts: []`인 항목도 같습니다.
`input_hash`는 추출에 쓰는 원본 ID·청크 위치·본문·제목·섹션·카테고리를 반영합니다.
입력이 바뀌면 청크가 다시 pending이 되며, 오래된 해시 쌍으로 저장하면 거절됩니다.

### 파라미터 레퍼런스

`src/pkb/mcp_server.py`에 등록된 그대로의 시그니처:

```python
search_knowledge(query, category="", top_k=5, include_archived=False,
                 include_obsidian=True, query_variants=[], profile="all",
                 canonical_group=True, canonical_boost=0.15,
                 expand_context=None, max_context_tokens=4000,
                 doc_ids=None, source_types=None, people=None,
                 date_from=None, date_to=None, path_prefix=None)
write_file(file_path, content, ingest=False, dry_run=False,
           expected_hash="", strict_policy=True)
list_documents(category="", include_archived=False, limit=50)
list_categories(include_archived=False)
related_documents(doc_id, top_k=5, include_archived=False, doc_ids=None, source_types=None,
                  people=None, date_from=None, date_to=None, path_prefix=None)
list_files(directory="data", offset=0, limit=100)
get_file(file_path)
put_attachment(path, data_base64, offset, total_size, sha256="")
get_attachment(path, offset=0, length=4194304)
list_media(kinds=None, missing_only=False, limit=200, offset=0)
get_media(doc_id, offset=0, length=4194304)
put_media_embeddings(doc_id, kind, sha256, model, segments)
search_media(query, kinds=None, top_k=12, include_archived=False, doc_ids=None, people=None,
             date_from=None, date_to=None, path_prefix=None)
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

검색은 `analyze`, `issues`, `needed_sources`와 법률 필터 `as_of`, `law_id`, `article_id`, `case_id`, `legal_version`, `legal_kind`도 받는다. 질문 계획과 법률 출처 메타데이터는 문맥 토큰 예산에 포함된다. 답변 소비자는 사실 주장별로 인용하고, 근거 부족 시 보류하며, 상충 출처의 시점·조건을 함께 설명한다. [평가 절차](../../evaluation/README.md)와 [법률 메타데이터](../legal-metadata.md)를 참고한다.
