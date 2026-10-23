# 첨부파일을 PKB 문서로 가져오기

`put_attachment`로 업로드를 완료한 뒤 반환된 상대경로와 SHA-256을
`import_attachment(path, sha256, category, title="")`에 전달한다. `category`는 기존 코퍼스의
최상위 카테고리 이름 하나다. 예: `study`. 새 카테고리나 하위 경로를 만들지는 않는다.

```json
{
  "path": "imports/report.pdf",
  "sha256": "<업로드가 반환한 64자리 SHA-256>",
  "category": "study",
  "title": "보고서"
}
```

도구는 원본을 변경하지 않고 `data/<category>/research/import-<sha256>.md`를 만든 다음
동기적으로 색인한다. 원본 첨부 읽기와 대상 문서 쓰기 권한이 모두 필요하다. 상대경로만
허용하며, 숨김 경로·상위 이동·심볼릭 링크·일반 파일이 아닌 입력을 거부한다.
원본 첨부는 업로드와 같은 최대 2 GiB다. 텍스트 추출은 최대 50 MiB의 지원 형식만
대상으로 하며, 생성 문서와 추출 텍스트도 최대 50 MiB다. 설정에 지정한 신뢰된 루트의
심볼릭 링크 별칭은 해소해 사용하고, 그 아래의 첨부·문서 경로에 있는 링크는 거부한다.
변환은 해시를 검증한 임시 사본에서 실행한다.

PDF, DOCX, PPTX, XLSX, Markdown, TXT, HTML은 기존 PKB 변환기로 텍스트를 추출한다.
이미지·오디오·기타 형식과 50 MiB를 넘는 지원 형식은 `extraction: unsupported`로
메타데이터 카드만 색인한다. 크기로 생략한 경우 응답과 카드에 이유를 표시한다.
이 경우 원본은 스트리밍으로 해시를 확인하며 변환용 사본을 만들지 않는다.
OCR·이미지 해석·음성 전사는
수행하지 않는다. 스캔 PDF처럼 추출 텍스트가 비어 있는 경우에도 그 상태를 명시한
메타데이터 카드를 만든다. 카드의 파일명·제목·원본 경로·해시는 검색할 수 있지만
추출하지 않은 이미지나 오디오의 내용까지 검색되는 것은 아니다.

성공하면 다음 필드를 가진 JSON 문자열을 반환한다.

| 필드 | 의미 |
| --- | --- |
| `doc_id` | 생성·색인한 Markdown 문서 ID |
| `attachment_path` | `ATTACHMENTS_ROOT` 아래 원본 상대경로 |
| `sha256` | 검증한 원본 SHA-256 |
| `title` | 저장된 제목 |
| `extraction` | `succeeded`, `unsupported`, `empty` 중 하나 |
| `indexing` | 실제 색인 처리 완료 후 `succeeded` |
| `graph` | `queued`, `running`, `retrying`, `waiting`, `completed`, `disabled`, `not_applicable` |
| `graph_status` | 문서 ID, 현재 입력의 전체·완료·미완료·실패 청크 수, 모델과 상태 설명 |
| `chars` | 추출한 텍스트의 문자 수; 내용 추출이 없으면 0 |
| `message` | 내용·메타데이터 색인 여부와 검토 상태 설명 |

가져온 문서는 `doc_type: research`, `authority: imported`, `status: draft`,
`source_type: document`로 저장한다. `source`, `original_file`, `attachments`, `source_id`에
원본 경로·해시를 남긴다. 미검토 문서는 `profile="all"`에서 검색할 수 있고,
`curated`와 `evidence`에서는 제외된다. 검토 후 일반 문서 편집 절차로 상태를 변경한다.
본문이 추출된 문서는 독립 `pkb graph worker`가 설정된 Ollama로 개념·관계를 추출한다.
`GRAPH_AUTO_EXTRACT=true`와 실행 중인 worker가 필요하다. 가져오기는 모델 응답을 기다리지 않는다.
`graph_extraction_status(doc_id)`로 현재 문서의 진행을 확인하며, `pending_chunks`는
재시도할 `failed_chunks`를 포함한다. 본문이 없는 사진·오디오·스캔 PDF 등의 카드는
`not_applicable`이며 OCR·전사 없이 파일명만으로 내용의 그래프를 만들지 않는다.
그래프 서비스가 잠시 중단돼도 완료된 업로드·색인은 보존되고 연결 복구 후 계속한다.

같은 원본 경로·해시·카테고리로 재시도하면 기존에 저장한 문서를 그대로 재색인한다.
제목을 다르게 전달해도 기존 제목을 유지한다. 전체 생성 기록의 해시를 검사하므로
기존 문서를 사람이 수정했거나 같은 해시에 다른 원본 경로가 기록되어 있으면 충돌로
거부한다. 기존 문서를 덮어쓰지 않는다.

실패는 기존 MCP 도구와 같이 `오류:`로 시작하며 `validation`, `extraction`, `save`,
`indexing` 중 실패 단계를 표시한다. 색인 실패 후에도 원본과 저장된 Markdown은
보존되므로 원인을 해결한 뒤 같은 요청으로 재시도할 수 있다. `오류:` 응답은 성공
JSON으로 해석하면 안 된다.
