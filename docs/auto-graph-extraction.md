# Windows Ollama 자동 그래프 추출

문서를 색인하면 별도 작업자가 새 본문과 변경된 본문을 찾아 개념·관계를 추출한다. 앱을 닫아도 작업은 계속된다. 기존 그래프를 초기화하지 않는다.

## 운영 구성

- 생성 서버: `http://192.168.0.2:11434`
- 생성 모델: `gemma4:12b-it-qat`
- 추출 설정: context 8192, output 2048, timeout 300초, 요청당 청크 1개
- 켜기: `.env`의 `GRAPH_AUTO_EXTRACT=true`
- 수동 실행: `uv run pkb graph worker --interval 20`
- 서비스 실행: `MeetingNotes.app/Contents/MacOS/MeetingNotes --graph-worker /Users/jk/apps/personal-docs`
- macOS 서비스: `~/Library/LaunchAgents/dev.jongkwan.pkb-graph-worker.plist`
- 로그: `~/Library/Logs/pkb-graph-worker.out.log`, `pkb-graph-worker.err.log`

이 Mac의 LaunchAgent는 PKB 자료함 실행 파일을 화면 없는 작업자 모드로 실행한다. 앱 프로세스가 부모로 남고, 기존 `pkb-storage-exec.sh`로 외장 APFS 데이터 볼륨을 확인한 다음 백엔드 `.venv/bin/pkb` 작업자를 시작한다. 같은 그래프 DB의 자동 작업자는 한 개만 실행된다. 종료 신호는 자식 작업자에 전달하며, 현재 요청을 마친 뒤 멈춘다. 재기동하면 완료하지 못한 입력부터 이어가며 파일이나 기존 추출 마커를 초기화하지 않는다.

### macOS 로컬 네트워크 권한

터미널 실행과 LaunchAgent는 로컬 네트워크 권한이 다르다. 이 작업자는 PKB 자료함의 백그라운드 서비스이므로 앱 실행 파일을 부모로 유지하고 plist의 `AssociatedBundleIdentifiers`도 `dev.jongkwan.meeting-notes`로 지정한다. 앱 빌드에는 `NSLocalNetworkUsageDescription`을 포함한다. 권한 창이 나타나면 PKB 자료함의 로컬 네트워크 접근을 허용해야 Windows Ollama에 연결된다. 화면이 잠겨 있으면 잠금을 해제한 뒤 확인한다. 시스템 설정의 개인정보 보호 및 보안 → 로컬 네트워크에서 바꿀 수 있다.

터미널에서 성공해도 서비스 로그에 `No route to host`가 있으면 자동 운영이 확인된 것이 아니다. 앱 연결 정보와 권한을 확인한 뒤 실제 서비스 프로세스의 저장 성공까지 검증한다. [Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)의 macOS considerations를 따른다.

```bash
uv run pkb graph worker-status
uv run pkb graph extraction-runs --limit 5
```

모델을 바꾸려면 `pkb graph models`로 설치된 생성 모델을 확인하고 `GRAPH_OLLAMA_MODEL`을 변경한 뒤 작업자를 재시작한다. 모델 이름과 digest, 입력 hash, 추출 옵션, 결과·실패 이력을 저장한다. 이미 완료한 문서를 모델 변경만으로 다시 추출하지는 않는다. 검색 임베딩 설정은 생성 모델과 별개다.

모델에는 긴 원본 경로 대신 요청 안에서만 유효한 짧은 문서 식별자를 보낸다. 출력 스키마와 실제 결과의 식별자·청크 번호를 검사한 뒤 정확한 원본 문서에 연결한다. 누락하거나 알 수 없는 식별자를 돌려준 결과는 저장하지 않고 재시도한다.

## 처리 순서와 재시도

1. 원본 파일 보관·업로드
2. 지원 문서의 본문 추출과 검색 색인
3. 자동 작업자가 색인된 현재 입력과 그래프의 완료 마커를 비교
4. Ollama 구조화 출력의 스키마·온톨로지·원문 일치 검사를 통과한 결과 저장
5. 앱의 개념 그래프 진행을 완료로 갱신

작업자는 20초마다 색인 UUID와 primary shard의 sequence checkpoint를 확인한다. 시작할 때와 색인이 바뀌었을 때만 전체 청크를 조회하며, 변경이 없으면 본문을 다시 읽지 않고 메모리에 보관한 청크 식별자·입력 hash와 SQLite 완료 마커를 비교한다. 처리할 청크의 본문만 다시 읽고 버전을 검사한다. 캐시에 본문이나 임베딩은 보관하지 않는다. 새 자료·메타데이터 변경·삭제·임베딩 세대 전환을 감지하며, 완료 마커가 초기화된 경우에도 다시 추출한다. 조회 중 색인이 바뀌거나 일부 검색만 성공하면 그 조회를 완료된 상태로 채택하지 않는다.

Windows 서버가 꺼지거나 통신·모델 출력 검사가 실패하면 해당 입력을 재시도한다. 재시도 간격은 30초부터 최대 1시간까지 늘어나며 재시작 후에도 유지된다. 실패한 한 문서가 다른 새 자료의 처리를 막지 않는다. 전체 evidence rebuild 중에는 자동 작업을 대기한다. 실행 중 원문이나 처리 대상 여부가 바뀌면 이전 입력의 결과를 저장하지 않고 현재 버전을 다음에 처리한다.

`graph_extraction_status(doc_id)`는 현재 문서의 상태와 `total_chunks`, `completed_chunks`, `pending_chunks`, `failed_chunks`를 반환한다. pending에는 failed가 포함된다. 완료는 현재 입력 전체의 저장 완료이며 별도의 사실 검증이나 편집 검토 완료를 뜻하지 않는다.

앱은 선택한 자료의 상태를 확인하고, 대기·추출·재시도 중에는 10초마다 갱신한다. 그래프 개요 탭에 다시 들어가면 현재 저장된 관계를 다시 읽는다.

## 처리 대상

새 업로드뿐 아니라 다른 경로로 색인한 신규·변경 문서도 대상이다. PDF·오피스 문서·웹 문서는 기존 파서에서 본문을 얻은 경우 자동 처리한다. 사진·오디오·텍스트 없는 스캔 PDF·지원하지 않는 형식은 내용 추출이 선행돼야 하므로 `not_applicable`로 표시한다. 파일명과 관리용 메타데이터를 분석된 본문으로 취급하지 않는다.

사진 OCR, 이미지 의미 분석, 음성 전사는 이 자동 그래프 작업자의 범위가 아니다. 미검토 가져오기 문서는 계속 초안으로 보관하며, 기존 curated/evidence 검색 정책을 유지한다.
