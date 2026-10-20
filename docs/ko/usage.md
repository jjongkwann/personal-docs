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
폴더 전체(도구 산출물), 레거시 백업을 복원했을 때의 폐기된 `_concepts/` 경로.

---

## 쓰기 규칙

Claude Code가 `write_file`로 새 문서를 저장하기 전에 지키는 규칙입니다.

- 저장 전 내용을 다듬으세요 — 읽을 수 있는 산문(제텔카스텐 톤), frontmatter(title·tags·필요시 expires_at) 포함.
- 배치 결정: ① 기존 주제 폴더 우선 (`list_documents`로 확인) ② 단발 조사는 해당 주제의
  `<주제>/research/`에 curated frontmatter 포함 저장 ③ 계획된 시리즈만 새 폴더 ④ 같은 주제가
  2~3건 쌓이면 폴더로 승격. 날짜는 파일명이 아니라 frontmatter에. `daily-research/`는 레거시
  인박스입니다: strict 모드에서 신규 저장은 거부되고, 기존 파일 편집만 허용됩니다.

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
uv run pkb query "RAG 평가" --expand 1           # 전후 1청크를 neighbors로 부착
uv run pkb query "BM25" --rerank                 # 이번 실행만 CrossEncoder 재순위 강제
uv run pkb query "BM25" --no-obsidian            # 코퍼스만, obsidian/* 문서 제외
uv run pkb query "BM25" --context-tokens 2000 --candidate-k 50
uv run pkb query "BM25" --fusion linear --lexical-weight 0.5 --rerank --rerank-context
```

`--top-k` 기본값은 `DEFAULT_TOP_K`(5)이고, `--rerank`/`--no-rerank`와 `--expand`는 생략 시 각각
`RERANK_ENABLED`·`EXPAND_CONTEXT` 설정값을 따릅니다. **부정형이 비대칭이라는 점에 주의** — 플래그
쌍은 `--no-include-obsidian`이 아니라 `--include-obsidian` / `--no-obsidian`입니다.
`--candidate-k`는 BM25·kNN 각각의 후보 수입니다. `--fusion linear`는 채널별 점수를 정규화한 뒤
`--lexical-weight`(0~1)로 가중합합니다. 기본 결합은 RRF입니다. `--rerank-context`는 재순위가
켜졌을 때 제목·섹션을 모델 입력에 더합니다. `--context-tokens`는 출처와 주변 문맥을 포함한
출력 토큰 수를 제한합니다. 정본 점수 보정 `--canonical-boost`는 0 이상 1 미만입니다.

재현 가능한 실험에는 `EMBEDDING_REVISION`·`RERANK_REVISION`에 모델 커밋을 지정하세요.
임베딩 revision을 바꾸면 fingerprint가 달라져 다음 sync에서 해당 벡터를 다시 계산합니다.
설정하지 않으면 기존 fingerprint를 유지합니다.

## 문서 관리

```bash
uv run pkb list
uv run pkb list --category study
uv run pkb list --limit 0                        # 0 = 전체 (기본 50)

uv run pkb show data/study/rag-overview.md                     # 메타 + 목차
uv run pkb show data/study/rag-overview.md --content           # 청크 본문 포함
uv run pkb show data/study/rag-overview.md --content --chunks "3-7"   # 3~7번 청크만

uv run pkb write data/writing/note.md --content "..."   # 작성 + 자동 인제스트 (--content 생략 시 stdin)
uv run pkb write data/writing/note.md --no-ingest       # 저장만 하고 나중에 인제스트

uv run pkb reindex-doc data/study/rag-overview.md       # 외부에서 편집한 문서 하나만 재인제스트
uv run pkb delete data/study/rag-overview.md
```

`write`·`show`·`reindex-doc`는 각각 `write_file`·`get_document`·`reindex_document`의 CLI 쌍둥이로,
동일한 코어 경로와 동일한 `data/` 하위 제한을 공유합니다.

### 변경 감시

```bash
uv run pkb watch                 # 10초 간격 폴링
uv run pkb watch --interval 60   # 60초 간격 폴링
```

시작 시 `sync`와 동일한 reconcile을 한 번 수행해 기준선을 잡고, 이후에는 변경된 파일만 델타
인제스트합니다(mtime + size 스냅샷 비교). 종료는 Ctrl+C.

---

## 인덱스 재생성

매핑 스키마가 바뀌었거나 (예: `section_path` 필드 추가) 인덱스가 꼬였을 때:

```bash
uv run pkb reindex           # 확인 프롬프트 있음
uv run pkb reindex --yes     # 바로 실행
```

데이터 코퍼스(DATA_ROOT)와 OBSIDIAN_PATH 전체를 새로 인덱싱합니다.

### 무중단 전환 (`index-switch`)

`reindex`는 제자리에서 삭제 후 재생성하므로 실행 중에는 검색 품질이 떨어집니다. 전량 재임베딩을
강제하는 `EMBEDDING_MODEL`/`EMBEDDING_DIMS` 변경 시에는 새 **물리** 인덱스를 먼저 채운 뒤 읽기
alias를 원자적으로 전환하세요:

```bash
# 1. 이번 실행에만 ES_INDEX를 덮어써서 새 물리 인덱스를 채움
ES_INDEX=pkb_documents_v2 uv run pkb reindex --yes

# 2. 읽기 alias를 원자 전환 (이전 대상 출력)
uv run pkb index-switch pkb_documents_v2
```

`ES_INDEX`는 물리 인덱스가 아니라 **읽기 alias**로 취급됩니다. `index-switch`는 대상 인덱스가
없거나, alias 이름과 같거나, `ES_INDEX`가 alias가 아닌 물리 인덱스일 때 명시적으로 실패합니다
(`store.switch_read_alias`). 이전 물리 인덱스는 절대 자동 삭제하지 않으므로, 새 인덱스를 검증한
뒤 직접 지우면 됩니다.

문서 단위 색인은 청크 색인을 따라갑니다. 새 청크 색인을 만들 때 `<ES_INDEX>_docs`도 함께
생기고(위 1단계가 `pkb_documents_v2_docs`도 채움), `index-switch`가 `<alias>_docs` alias를 같은
요청에서 옮깁니다. 대상의 문서 색인이 없으면 실패합니다.

### 문서 단위 색인 (`doc-index`)

```bash
uv run pkb doc-index
```

`<ES_INDEX>_docs`가 없으면 만들고, 청크 색인에서 문서마다 한 행(메타데이터, 청크 수,
`doc_vector` = 저장된 청크 임베딩 평균의 L2 정규화 — 재임베딩 없음)을 다시 씁니다. 청크 색인에
없는 문서의 행은 지웁니다. 처음 한 번 실행한 뒤에는 인제스트·삭제·아카이브·복구·purge가 행을
함께 고칩니다. `list_categories`와 `related_documents`에 필요하고, `list_documents`는 있으면 씁니다.

### 작은 서버의 임베딩 모델 전환 (`reembed`)

메모리 8GB 서버는 코퍼스 전체를 다시 임베딩하지 못합니다. GPU가 있는 머신에서 SSH 터널로 새
물리 인덱스를 채우고, 개념 그래프 벡터는 사본에서 다시 계산한 뒤, 서버를 멈춘 상태에서 전환합니다.

```bash
uv run pkb reembed --target <새 물리 인덱스> [--source <인덱스 또는 alias, 기본 ES_INDEX>]
                   [--batch-size 256] [--max-batches 0] [--dry-run]
uv run pkb graph reembed [--db <그래프 sqlite, 기본 GRAPH_DB_PATH>] [--batch-size 256] [--max-batches 0]
```

`reembed`는 원본의 모든 청크를 같은 `_id`·필드로 대상에 복사하면서 `embedding`과
`embedding_fingerprint`만 현재 설정으로 바꿉니다. fingerprint는 같은 설정에서 ingest가 계산하는
값이라 서버의 다음 `sync`가 이 벡터를 그대로 씁니다. 다시 실행하면 끝난 청크는 건너뜁니다.
그사이 원본이 바뀌었으면 바뀐 청크만 다시 임베딩하고, 메타데이터만 바뀐 청크는 기존 벡터로
복사하고, 원본에서 사라진 청크는 지웁니다. 이미지·오디오 청크(`media_model`이 있는 청크) 중
`media_model`이 `MEDIA_EMBEDDING_MODEL`과 같은 것은 미디어 모델의 벡터를 그대로 두고 복사합니다. 다른
미디어 모델의 청크는 벡터 공간이 달라 복사하지 않고, 대상에 이미 있으면 지우며, `미디어 제외 N`으로
셉니다. 전환 뒤 `pkb media-sync`가 다시 만듭니다. 모든 청크가 들어가면 `<target>_docs`를 다시 채우므로
`index-switch`가 두 alias를 함께 옮길 수 있습니다. `--dry-run`은 할 일만 세고 모델을 올리지
않습니다. 종료 코드 3은 `--max-batches`에서 멈췄고 남은 일이 있다는 뜻입니다. `graph reembed`는
모든 개념 벡터를 `이름: 설명`(`graph_store_concepts`가 임베딩하는 문자열)으로 다시 계산하고, 묶음마다
커밋하며, 끝나면 `graph_meta`에 임베딩 식별자를 기록합니다. 기록과 설정이 다르거나 재계산이 끝나지
않았으면 어휘 조회, `graph_query`/`graph_map`의 의미 시드, 임베딩 병합, 새 개념 저장이 두 모델의
벡터를 비교하지 않고 실패합니다.

아래 절차는 어떤 모델로 바꾸든 같고, 예는 `google/embeddinggemma-2`(EmbeddingGemma 2)로 바꾸는
경우입니다. 2026-10-15 M1 Max에서 같은 한국어 gold 40문항, 같은 청크 15,440개로 벡터와 질의 임베더만
바꿔 잰 결과:

| 모델 | RRF MRR@5 | R@5 | nDCG@5 | dense만 MRR@5 | p50 지연 |
|---|---|---|---|---|---|
| `google/embeddinggemma-2` | 0.469 | 0.600 | 0.502 | 0.372 | 110 ms |
| `jinaai/jina-embeddings-v5-text-small` | 0.453 | 0.625 | 0.496 | 0.373 | 186 ms |
| `BAAI/bge-m3` | 0.485 | 0.575 | 0.508 | 0.325 | 62 ms |

한 문항이 R@5 0.025이므로 셋은 오차 안입니다. 리랭크는 셋 모두 MRR을 낮췄습니다. EmbeddingGemma 2는
Apache-2.0(jina v5 omni는 CC BY-NC)이고 글·이미지·오디오를 한 모델이 임베딩합니다. M1 Max에서 배치
32로 청크를 초당 약 10.5개 임베딩하고, 프로세스 RSS는 최대 약 2GB입니다.

1. GPU 머신에서 서버와 같은 커밋을 받아 `uv sync`하고 `eg2.env`를 씁니다. fingerprint가 모델·
   리비전·task·문서 프롬프트·정규화·제목 prefix를 포함하므로 `ES_HOST` 말고는 서버 `.env`와 같아야
   합니다:

   ```bash
   ES_HOST=http://127.0.0.1:19200
   ES_INDEX=pkb_current
   EMBEDDING_MODEL=google/embeddinggemma-2
   EMBEDDING_DIMS=768
   EMBEDDING_DTYPE=bfloat16
   EMBEDDING_TEXT_ONLY=true
   EMBEDDING_QUERY_PROMPT=SearchQuery
   EMBEDDING_DOCUMENT_PROMPT=Document
   EMBEDDING_NORMALIZE=true
   EMBED_CONTEXT_PREFIX=true
   MEDIA_EMBEDDING_MODEL=google/embeddinggemma-2
   ```

   이전 모델에만 필요한 설정은 두 머신의 `.env`에서 지웁니다(`source`한 변수는 `.env`를 덮어쓰지만
   `.env`의 다른 줄을 없애지는 않습니다). jina v5라면 `EMBEDDING_TRUST_REMOTE_CODE`와
   `EMBEDDING_TASK`이고, 서버는 `EMBEDDING_TASK`가 비어 있을 때만 EmbeddingGemma 2 미디어 벡터를
   받습니다. EmbeddingGemma 2는 `float16`에서 NaN이나 망가진 벡터를 내므로 GPU에서는 `bfloat16`이어야
   합니다(CPU는 항상 `float32`). `Document`는 청크를 `title: none | text: …`로 만듭니다. 청크 2,645개
   표본에서 `title: {title} | text: …`보다 나았으므로(MRR@5 0.713 대 0.640) 제목·섹션 prefix는 text
   안에 둡니다. `MEDIA_EMBEDDING_MODEL`은 `reembed`가 복사할 이미지·오디오 청크를 정합니다.
   `EMBEDDING_BATCH_SIZE`는 fingerprint에 들어가지 않습니다. `GRAPH_DEDUP_THRESHOLD`는 모델마다
   다르고(bge-m3 0.88, jina v5 small 0.89) EmbeddingGemma 2 값은 아직 맞추지 않았습니다.

2. 서버 Elasticsearch(`127.0.0.1:9200`에서 대기)로 터널을 열고, 서버가 기존 인덱스로 계속 서비스하는
   동안 새 인덱스를 채웁니다:

   ```bash
   ssh -N -L 19200:127.0.0.1:9200 <서버> &
   set -a; source eg2.env; set +a
   uv run pkb reembed --target pkb_documents_20261015_eg2
   ```

3. 서버에서 SQLite backup 명령으로 그래프를 복사하고 사본의 내용 해시를 남깁니다(`.sha3sum`은 파일
   바이트가 아니라 행을 해시합니다). 사본을 GPU 머신으로 옮겨 벡터를 다시 계산합니다:

   ```bash
   # 서버
   sqlite3 <GRAPH_DB_PATH> ".backup /tmp/pkb_graph.copy.sqlite"
   sqlite3 /tmp/pkb_graph.copy.sqlite .sha3sum > /tmp/pkb_graph.sha3
   # GPU 머신 (scp 뒤)
   uv run pkb graph reembed --db pkb_graph.copy.sqlite
   ```

4. 서버를 멈춥니다(`launchctl unload ~/Library/LaunchAgents/dev.jongkwan.pkb-mcp.plist`, `pkb watch`도).
   2단계의 `reembed`를 다시 실행해 그사이 쓰인 내용을 반영합니다. 몇 청크만 임베딩해야 정상입니다.
5. 그래프를 바꿉니다. `sqlite3 <GRAPH_DB_PATH> .sha3sum`이 `/tmp/pkb_graph.sha3`와 같아야 합니다.
   다르면 복사 뒤 그래프가 바뀐 것이니 3단계를 다시 합니다. 기존 파일은 남겨 둡니다:
   `mv <GRAPH_DB_PATH> <GRAPH_DB_PATH>.prev` 뒤 다시 계산한 파일을 그 자리에 둡니다.
6. 서버 코드를 갱신하고 `uv sync`합니다. EmbeddingGemma 2의 프로세서가 `EMBEDDING_TEXT_ONLY=true`에서도
   불러오는 torchvision이 함께 설치됩니다. `.env`의 임베딩 줄을 `eg2.env`의 줄(`ES_HOST` 제외)로 바꾸고
   이전 모델의 다른 설정을 지웁니다. 첫 검색이 모델을 내려받지 않게 한 번 올려 둡니다:
   `uv run python -c "from pkb.embeddings import embed_queries; embed_queries(['warmup'])"`.
7. 서버에서 `uv run pkb reembed --target pkb_documents_20261015_eg2 --dry-run`이
   `임베딩 0 · 복사 0 · 삭제 0`을 보여야 합니다. 서버 설정이 새 인덱스에 저장된 fingerprint를 그대로
   만든다는 확인이라, `sync`가 아무것도 다시 임베딩하지 않습니다. `미디어 제외 N`은 새 인덱스에
   들어가지 않는 이전 미디어 모델의 이미지·오디오 청크 수입니다.
8. `uv run pkb index-switch pkb_documents_20261015_eg2`를 실행하고 출력되는 이전 인덱스를 적어
   둡니다. 서버를 띄우기 전에 전환해야 합니다. 새 설정으로 이전 인덱스를 읽으면 차원이 다를 때(여기서는
   768과 1024) 벡터 검색이 실패하고, 같을 때(bge-m3와 jina v5 small은 둘 다 1024) 오류 없이 엉뚱한
   결과만 나옵니다.
9. 서버를 띄우고(`launchctl load ...`) 게이트웨이의 도구 목록을 갱신합니다. MCP ContextForge는
   `POST /gateways/<게이트웨이 id>/tools/refresh`입니다([MCP 연동](mcp.md) 참고).
10. 미디어를 임베딩하는 Mac에서 `uv run pkb media-sync --url https://<게이트웨이>/mcp`를
    실행합니다([이미지·오디오 벡터](#이미지오디오-벡터-media-sync) 참고). `reembed`가 이전 미디어 모델의
    청크를 옮기지 않았으므로, 끝날 때까지 `search_media`는 그 파일을 찾지 못하고
    `list_media(missing_only=True)`가 그 파일을 보여 줍니다.
11. 확인: `uv run pkb doctor`, `uv run pkb query` 몇 개, `uv run pkb graph query "..."`, 그리고 실제
    편집 말고는 `embedded=0 added=0`이 나와야 하는 `uv run pkb sync`.

되돌리기: 서버를 멈추고 `uv run pkb index-switch <이전 인덱스>`(그 `_docs` 인덱스는 남아 있음),
`.env`의 이전 줄과 `<GRAPH_DB_PATH>.prev` 파일을 되돌린 뒤 서버를 띄우고 게이트웨이를 갱신합니다.
전환 뒤 쓴 내용은 새 인덱스에만 있으므로 `uv run pkb sync`로 이전 인덱스에 넣습니다. 이전 인덱스에
이미지·오디오 청크가 남아 있지만, 서버는 `src/pkb/media.py`의 `MEDIA_SPACES`에 이전 미디어 모델이 있을
때만 그것을 검색합니다. 지금 코드에는 `google/embeddinggemma-2`만 있습니다. 이전 물리 인덱스와 그
`_docs`는 새 인덱스가 한동안 문제없이 돈 뒤 직접 지웁니다.

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

이미지·오디오는 `sync`가 인제스트하지 않지만, 파일이 남아 있는 미디어 색인은 "원본에 없는 문서"가
되지 않고 파일이 사라지면 다른 문서처럼 정리합니다(`ATTACHMENTS_ROOT`는 3단계). `sync`는 색인된
미디어의 노트 맥락도 다시 임베딩하지 않고 고칩니다 ([이미지·오디오 검색](mcp.md#이미지오디오-검색) 참고).

`reindex`(전체 재구축)와 달리 `sync`는 델타만 처리하므로 일상 유지보수용입니다. `doctor`가 설정-인덱스 불일치(연동 껐는데 문서 잔존)를 경고합니다.

### 이미지·오디오 벡터 (`media-sync`)

서버는 모델의 텍스트 탑만 올리고 이미지·오디오 인코더는 올리지 않습니다. 메모리가 넉넉한 Mac에서 새로 생기거나 바뀐 이미지·녹음을
임베딩해 서버로(또는 MCP 게이트웨이를 거쳐) 올립니다:

```bash
uv sync                                                  # torchvision 포함 (서버에도 필요)
uv run pkb media-sync --url https://<게이트웨이>/mcp --dry-run   # 보낼 파일 목록만
uv run pkb media-sync --url https://<게이트웨이>/mcp             # 임베딩해 올리기
uv run pkb media-sync --kinds audio --limit 20                  # 한 종류, 앞의 20개
```

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--url` | `http://127.0.0.1:8787/mcp` (`PKB_MCP_URL`) | PKB MCP 서버나 게이트웨이 |
| `--token-file` | `~/.config/mcp-gateway/token` | Bearer 토큰. `https://` 주소에만, 파일이 있을 때만 보냄 |
| `--kinds` | `image,audio` | 처리할 종류 |
| `--limit` | `0` (전부) | 이번 실행의 최대 파일 수 |
| `--dry-run` | 꺼짐 | 내려받거나 임베딩하지 않고 목록만 출력 |
| `--device` | `auto` | `mps`, `cuda`, `cpu` |

파일마다 한 줄을 찍고, 끝에 모델 적재 시간·이미지당 초·30초 오디오 구간당 초를 보여 줍니다. 지금
해시와 모델로 이미 색인된 파일은 건너뛰므로 여러 번 돌려도 됩니다. 오디오는 macOS `afconvert`,
SVG·HEIC는 `sips`가 필요하고, 서버는 [모델 전환 절차](#작은-서버의-임베딩-모델-전환-reembed) 1단계의
EmbeddingGemma 2 설정과 함께 `MEDIA_EMBEDDING_MODEL=google/embeddinggemma-2`이어야 합니다.

새 파일도 찾히게 하려면 `StartInterval` 3600인 LaunchAgent로 위 명령을 주기적으로 돌립니다. 배터리에서는
돌지 않도록 스크립트 첫 줄에서 전원을 확인합니다 (`pmset -g ps | grep -q "AC Power" || exit 0`).
새 파일이 없으면 목록만 받고 모델을 올리지 않고 끝납니다.

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

개념 그래프 구축은 Claude Code가 MCP의 `graph_list_chunks`/`graph_store_concepts`로 직접 추출·저장합니다(API 호출 없음). CLI는 통계·조회 질의 4종·Evidence Map 스냅샷·evidence 재구축을 제공합니다.

```bash
# 현재 그래프 통계
uv run pkb graph stats

# 조회 질의 — MCP graph_* 도구와 동일한 모델을 사용
uv run pkb graph explain "BM25"                 # 개념 하나의 양방향 엣지 + 근거
uv run pkb graph path "BM25" "RRF"              # 제한된 최단 경로
uv run pkb graph query "키워드 검색과 벡터 검색은 어떻게 연결돼?"   # 의미 시드 하위 그래프
uv run pkb graph affected "BM25" --relation prerequisite_of  # 저장 방향 하위 순회

# 온톨로지(TBox) — docs/ko/graph-rag.md의 "온톨로지 (TBox)" 참고
uv run pkb graph ontology                       # kind, 관계 특성·시그니처
uv run pkb graph inferred "BM25" --relation is_a  # 전제 경로가 붙은 추론 상위 개념
uv run pkb graph validate                       # hard/soft 보고서. hard 위반이 있으면 종료 코드 1
uv run pkb graph export --out graph.ttl         # OWL TBox + SKOS 개념 (--format trig는 추론 폐포 포함)

# 오프라인 Evidence Map HTML (전체 옵션 표는 docs/ko/graph-rag.md 참고)
uv run pkb graph map --concept "BM25" --open

# 임베딩 모델을 바꾼 뒤 모든 개념 벡터 재계산 (재실행하면 이어서, "임베딩 모델 전환" 참고)
uv run pkb graph reembed --db path/to/pkb_graph.copy.sqlite

# 구 append-only 관계를 evidence 기반으로 전량 재구축할 때만 실행
# 기존 그래프는 유지하고 staging evidence·추출 마커만 초기화
uv run pkb graph reset-evidence --yes

# 선택: 설치된 Ollama 모델로 pending 전량 자동 추출 (중단 후 재실행 가능)
uv run pkb graph rebuild-evidence-local --yes
#   --model gpt-oss:20b            # 설치된 Ollama 생성 모델
#   --batch-size 8                 # 생성 1회당 청크 수 (1~8)
#   --max-batches 0                # 0=완료까지, 1=샘플 검증
#   --endpoint http://127.0.0.1:11434   # 로컬 Ollama 엔드포인트

# 수동 graph_list_chunks → graph_store_concepts 전량 처리 후 pending=0일 때 원자 전환
uv run pkb graph finalize-evidence --yes
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
없습니다. 이 경로로 인제스트된 파일은 `doc_id`가 `obsidian/<상대경로>`로 저장되고, **카테고리는
볼트 최상위 폴더명에서 동적으로 결정**됩니다 — `data/`와 같은 규칙(`ingest.py:_extract_category`)이라
`obsidian/career/x.md`는 `category=obsidian`이 아니라 `category=career`가 됩니다. 볼트 루트에 직접
놓인 파일은 `misc`로 떨어집니다. `DATA_ROOT` 서브트리는 크롤에서 자동 제외되어 이중 인제스트가
없습니다.

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
    ├── operations.py        # CLI·MCP가 공유하는 작성·변환·동기화 도메인 코어
    ├── documents.py         # 문서 경로 해석, 조회, 생명주기
    ├── config.py            # 설정 관리
    ├── ingest.py            # 파싱, 청킹 (PDF는 pdfminer 페이지 보존, 그 외 markitdown)
    ├── embeddings.py        # sentence-transformers 임베딩
    ├── rerank.py            # CrossEncoder 재순위
    ├── store.py             # Elasticsearch CRUD, 인덱스 관리
    ├── retrieve.py          # 하이브리드 검색 (BM25 + kNN + RRF)
    ├── report.py            # doctor 상태 리포트
    ├── eval.py              # 검색 품질 평가 (모드별 recall/MRR)
    ├── search_log.py        # 검색 호출 JSONL 로깅
    └── graph/               # SQLite Graph RAG (schema, store, query, notes, rebuild, viewmap)
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
| `EMBEDDING_DEVICE` | `auto` | 임베딩 torch 디바이스. `auto`는 mps → cuda → cpu 순 선택, `cpu`/`mps`/`cuda` 고정 가능 |
| `EMBEDDING_DTYPE` | `float16` | GPU 디바이스(mps/cuda)에서 임베딩 모델 가중치의 정밀도: `float16`, `bfloat16`, `float32`. CPU는 항상 `float32`. `float16`은 메모리의 가중치를 절반으로 줄이며 재색인이 필요 없음. 되돌리려면 `float32`. EmbeddingGemma 2는 `float16`에서 NaN이나 망가진 벡터를 내므로 `bfloat16`을 씀 |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder 리랭커 모델 |
| `RERANK_DEVICE` | `auto` | 리랭커 torch 디바이스 (동일한 해석 규칙) |
| `RERANK_BATCH_SIZE` | `8` | CrossEncoder 배치 크기. MPS에서는 작은 배치가 더 빠른 것으로 측정됨 |
| `RERANK_ENABLED` | `false` | 리랭크 기본 사용 여부 |
| `CANDIDATE_K` | `20` | RRF/리랭커 후보 수. 기본값은 rerank 경로 벤치마크에서 `ck=50` 대비 latency 2.4x↓·품질 동일로 채택. 같은 벤치에서 RRF-only 경로도 `ck=20`이 nDCG/MRR 미세 우위. 더 큰 후보 풀이 필요하면 50으로 상향. |
| `EXPAND_CONTEXT` | `0` | N>0이면 검색 결과 전후 N 청크를 neighbors로 부착 (parent context) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 500 / 100 | 고정 크기 청킹 |
| `GRAPH_DB_PATH` | `data/.graph/pkb_graph.sqlite` | SQLite 개념 그래프 파일 |
| `GRAPH_DEDUP_THRESHOLD` | `0.88` | 개념 병합 임베딩 유사도 임계값 (bge-m3 기준. jina v5 small은 `0.89`가 같은 병합 비율) |
| `DEFAULT_TOP_K` | `5` | `pkb query`·`search_knowledge` 기본 결과 수 |
| `MCP_PORT` | `8787` | 공유 HTTP MCP 서버 포트 |
| `MCP_HOST` | `127.0.0.1` | 바인딩 주소. 서버를 다른 머신에 두고 신뢰하는 LAN/VPN에서 붙을 때만 `0.0.0.0`으로. `ACCESS_POLICY`가 없으면 인증이 없고 `write_file`이 볼트 어디든 쓸 수 있음. |
| `ACCESS_POLICY` | (비어 있음) | 접근 제어 정책 YAML 경로([접근 제어](access-control.md)). 비우면 단일 사용자 모드로 모든 호출이 관리자. 설정하면 Bearer 토큰이 필요하고 경로 규칙이 읽기·쓰기를 정함 |
| `MCP_PUBLIC_URL` | (비어 있음) | 클라이언트가 접속하는 MCP 주소. OAuth 메타데이터에 보호 자원으로 알린다. 비우면 `http://MCP_HOST:MCP_PORT/mcp` |
| `WARMUP_ON_START` | `false` | MCP 서버 기동 시 모델 예열. 끄면 첫 검색까지 서버가 ~50MB로 유휴 |
| `MODEL_IDLE_SECONDS` | `300` | 마지막 사용 뒤 이 시간(초)이 지나면 임베딩·리랭커 모델을 내려 메모리를 돌려줍니다. 다음 호출 때 다시 올립니다. `0`이면 계속 유지 |
| `EMBEDDING_TRUST_REMOTE_CODE` | `false` | 모델 저장소의 코드를 실행 (`jinaai/jina-embeddings-v5-text-small`에 필요, EmbeddingGemma 2는 불필요). 믿는 모델에만 |
| `EMBEDDING_TASK` | (빈 값) | `encode(task=...)` 어댑터 이름. jina v5는 `retrieval`. EmbeddingGemma 2는 비움 |
| `EMBEDDING_QUERY_PROMPT` / `EMBEDDING_DOCUMENT_PROMPT` | (빈 값) | 검색 질의 / 색인 텍스트의 `prompt_name`. EmbeddingGemma 2는 `SearchQuery` / `Document` |
| `EMBEDDING_NORMALIZE` | `false` | `encode`에 `normalize_embeddings=True` (EmbeddingGemma 2는 `true`). task·문서 프롬프트·정규화는 임베딩 fingerprint에 들어가 바꾸면 다음 sync에서 재임베딩 |
| `EMBEDDING_TEXT_ONLY` | `false` | 비전·오디오 인코더 없이 모델을 올림. EmbeddingGemma 2는 740M 대신 약 270M 파라미터만 올라가고 질의 벡터는 전체 모델과 같음(코사인 1.00). fingerprint에 들어가지 않음 |
| `ATTACHMENTS_ROOT` | (빈 값) | `put_attachment`/`get_attachment`가 쓰는 볼트 밖 폴더 (예: 회의 오디오). 비우면 두 도구가 꺼짐 |
| `MEDIA_EMBEDDING_MODEL` | (빈 값) | `put_media_embeddings`가 받는 이미지·오디오 벡터의 모델: `google/embeddinggemma-2` (`EMBEDDING_MODEL=google/embeddinggemma-2`, 빈 `EMBEDDING_TASK` 필요). 비우면 미디어 색인과 `search_media`가 꺼짐 |

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

v2 골드셋을 실제 `hybrid_search` 경로에 넣어 평가합니다. 질문 하나에 관련 문서·정확한 청크를
여러 개 지정하거나 답할 수 없는 질문으로 표시할 수 있습니다. 이름별 설정을 같은 `--top-k`
기준에서 비교합니다.

```bash
uv run pkb eval                            # 기본: <DATA_ROOT>/.eval/gold.jsonl
uv run pkb eval --gold legacy.jsonl --migrate-to new-v2.jsonl
uv run pkb eval --gold new-v2.jsonl --configurations file.json --output new-report.json --top-k 5
```

마이그레이션은 원본을 보존하고 새 파일을 만듭니다. 기존 `{"query":"...","doc_id":"..."}` 행은
문서 정답 하나를 가진 v2 행으로 바뀝니다. 필요한 정확한 청크나 다른 관련 출처는 검토 후 추가하세요.
`--output`도 새 파일 경로를 요구하며 질문별 결과와 실행 메타데이터를 저장합니다.

`file.json`은 설정 이름과 `hybrid_search` 옵션을 매핑합니다.

```json
{
  "baseline": {},
  "linear": {"fusion": "linear", "lexical_weight": 0.5},
  "rerank": {"rerank": true, "rerank_context": true}
}
```

### 골드셋 작성 지침

`data/.eval/gold.jsonl` — 라인당 한 항목:

```jsonl
{"version":2,"query":"BM25는 검색어의 점수를 어떻게 계산하나?","query_type":"paraphrase","answerable":true,"relevant":[{"doc_id":"data/study/rag/bm25.md","chunk_index":2},{"doc_id":"data/study/rag/bm25.md","chunk_index":3},{"canonical_id":"bm25-reference"}]}
{"version":2,"query":"존재하지 않는 프로젝트의 출시일은?","query_type":"no_answer","answerable":false,"relevant":[]}
```

- `query_type`별 집계가 보고서에 나옵니다. `variants`는 선택적 쿼리 변형 목록이며, 지정된 행에만
  적용됩니다. 관련 항목에는 `doc_id` 또는 `canonical_id`와 선택적 양수 `relevance`를 넣습니다.
  `chunk_index`는 `doc_id`와 함께 써야 하며, 같은 문서의 서로 다른 청크도 별도로 표시할 수 있습니다.
- 최종 recall과 nDCG는 관련 문서를 한 번만 셉니다. 근거 recall은 표시한 `doc_id` +
  `chunk_index` 쌍을 정확히 확인합니다. 답 없음 오탐률은 답할 수 없는 질문에 결과가 하나라도
  나온 비율이며, 보정된 답변 보류 능력의 척도는 아닙니다. 보고서에는 MRR과 p50/p95 지연도
  포함됩니다. 첫 실행은 모델 적재 시간이 지연을 좌우할 수 있으므로 정상 실행 지연을 비교할 때는
  모델을 예열하세요.
- Claude Code 세션이 대상 문서를 직접 읽고 질문을 작성하게 하세요. 이때 **제목 단어 금지** —
  파일명·제목의 단어를 그대로 쓰면 BM25가 공짜로 맞혀 변별력이 사라집니다. 내용을 패러프레이즈한
  질문이어야 모드 간 차이가 드러납니다.
- `data/.eval/`은 별도 설정 없이 기존 규칙으로 자동 커버됩니다: `.`으로 시작하는 폴더는
  인제스트에서 제외되어 골드셋이 검색 대상에 섞이지 않고, `data/`는 gitignore라 커밋되지 않습니다.

### 질문 분석·법률 기준일·답변 평가

`query --analyze`는 쟁점·기준일·필요 자료·검색어 변형·필터·모호성을 함께 표시한다.
`--issue`, `--needed-source`, `--query-variant`를 반복 전달해 복합 질문을 명시할 수 있다.
자연어 분석은 보수적인 규칙 기반이다. 일반 기술 질문에서 추출한 기준일은 계획에 보존하되,
지원하지 않는 날짜 필터로 기술 문서를 배제하지 않는다.

```bash
uv run pkb query "2025-12-31 기준 제7조 통지 기간?" --analyze --law-id verified-source-id
uv run pkb query "요건과 판결" --analyze --issue "법률 요건" --issue "판결의 적용" \
  --needed-source statute --needed-source judgment --as-of 2025-12-31
```

명시적인 `--as-of YYYY-MM-DD`는 법률 자료용이다. 법령은 시행 시작일 이상·종료일 미만,
판결은 선고일 이하를 검색하며 날짜 미상은 제외한다. 과거 기준일에는 현재 아카이브·만료로
역사 자료를 제외하지 않는다. 법령·조문·판결·버전 필터는 `--law-id`, `--article-id`,
`--case-id`, `--legal-version`, `--legal-kind`로 전달한다. BM25/kNN·변형·이웃 청크에 같은
필터를 적용하며 법률 버전은 정본 그룹에서도 구분한다. [저장·마이그레이션](../legal-metadata.md)을 참고한다.

[평가 자료와 절차](../../evaluation/README.md)는 정확한 문단, 답 없음, 여러 문서, 상충 자료를
포함한다. 기존 v2 자료는 그대로 읽고, 답변용 행에만 `id`, `expected_claims`, `answer_policy`를
추가한다. `pkb eval`은 실제 전달한 토큰 예산 내 문맥·순위·질문 계획을 해시와 함께 보존한다.
`use_variants=false`로 수동 변형도 끄며 `analyze=true`로 계획을 켠다. 리랭커 기본값은 꺼짐이다.

정답 라벨을 보지 않은 소비자의 답변과 별도 검토자의 주장별 판정을 저장한 다음 재생한다.

```bash
uv run pkb eval --gold data/evaluation/gold.jsonl \
  --replay-report data/evaluation/retrieval-report.json \
  --answers data/evaluation/reviewed-answers.jsonl --output data/evaluation/answer-report.json
```

이 단계는 재검색하지 않는다. 검색 Recall/nDCG와 답변 정확도·Citation precision·누락·보류를
따로 집계한다. 인용문이 실제 보이는 문맥에 존재하고 검토자가 해당 주장 지지를 확인해야
Citation precision의 정답으로 센다. 판정 없는 답변 지표는 미측정으로 남기며 해시 불일치는 거부한다.
