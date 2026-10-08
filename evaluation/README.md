# 검색과 답변 근거 평가

운영 검색은 `pkb eval`로 실행한다. 검색 응답을 읽는 외부 MCP 에이전트가 답변 소비자다. 저장소 자체에 답변 생성 모델은 없으므로 검색 지표를 답변 정확도로 대체하지 않는다.

```sh
uv run pkb eval --gold data/evaluation/gold.jsonl \
  --configurations evaluation/configurations.json \
  --output data/evaluation/retrieval-report.json
```

`configurations.json`의 각 설정은 `baseline`과 한 요소만 다르다: 질문 분석(`analyze`), 이웃 청크(`neighbors`), CrossEncoder(`rerank`), gold 변형(`oracle_variants`). `paired_comparisons`는 모든 설정을 첫 설정과 짝지으므로, 두 요소를 함께 바꾼 설정의 답변 차이는 한 요소에 귀속할 수 없다. gold의 `variants`는 근거 문단을 읽은 작성자가 근거 문구로 만든 검색어라 실제 질의가 아니다. `oracle_variants`는 검색어가 근거와 일치할 때의 상한을 보는 진단용이며 개선 근거로 쓰지 않는다. 리랭커는 평가 대상이며 활성화만으로 개선을 뜻하지 않는다. 결과는 모델·색인·코드·gold와 실제 전달한 근거 컨텍스트의 해시에 묶어 보존한다.

## 실제 코퍼스 gold

개인 기술 학습 자료의 경로·문단이 포함된 gold는 Git에서 제외되는 `data/evaluation/gold.jsonl`에 둔다. 외부 볼트의 기존 `.eval` 자료와 원문은 수정하지 않는다. 현재 준비한 보강 gold는 2026-10-08 live Elasticsearch 원문을 읽어 작성한 17문항이다.
기존 40문항은 `data/evaluation/existing-gold-v2.jsonl`로 원본을 보존해 변환했고,
기존 40 + 보강 17문항의 합본은 `data/evaluation/gold-combined-v2.jsonl`이다.
주장·인용 라벨은 보강 17문항에 있으므로 답변 비교는 이 부분집합을 별도로 보고한다.

| 유형 | 문항 수 | 검증 목적 |
| --- | ---: | --- |
| 정확한 근거 문단 | 9 | 맞는 문서 안에서도 실제 답을 담은 청크를 찾는가 |
| 여러 문서 | 4 | 하나의 결합 주장에 필요한 두 근거를 모두 전달하고 인용하는가 |
| 상충하는 서술 | 1 | 일반 재순위 권고와 특정 파이프라인의 하락 반례를 범위와 함께 제시하는가 |
| 답 없음 | 3 | 존재하지 않는 제품·실험·미래 실측을 만들어 답하지 않는가 |

실제 gold의 `content_hash`는 전체 색인 청크의 SHA-256이고 `quote`는 그 청크의 정확한 부분문자열이다. `expected_claims`는 답변 평가자가 사용하는 라벨이며 답변 생성자에게 전달하지 않는다. 여러 문서 문항은 각 결합 주장에 근거 두 개를 요구한다. 답 없음 문항은 현재 스냅샷에서 고유 제품명·실험명·미래 날짜의 `match_phrase` 결과 0건을 확인했다. 이는 해당 질문이 현재 자료로 답할 수 없다는 라벨이며 일반적인 무응답 판정 모델 성능을 대표하지 않는다.

상충 문항은 서로 다른 모델과 지표의 문서 서술을 비교한다. 일반적인 정확도 증가 설명과 특정 BGE-Large 파이프라인의 nDCG 하락은 재순위의 무조건적 개선을 주장할 수 없게 하지만, 동일 실험의 수치가 정면으로 모순되는 경우와 구분해야 한다. 명시적인 동일 쟁점의 상반된 자료는 아래 가상 법률 fixture에 따로 제공한다.

## 공유 가능한 가상 법률 fixture

`fixtures/legal/corpus.jsonl`의 5개 청크와 `fixtures/legal/gold.jsonl`의 5문항은 **전부 가상**이다. 실제 법령·사건·법률 결론을 나타내지 않는다. 실제 코퍼스 조사에서 법률·법령·판례가 들어간 문서 식별자는 발견되지 않았으므로 법률 사실을 새로 만들지 않았다.

fixture는 30일→10일 개정, 시행일 경계, 같은 기준일에 10일/20일로 다른 사본, 시행 전 질문, 기준일 이후 판결과 날짜 미상 사본을 포함한다. `effective_to`는 종료일을 포함하지 않는다. `original_url`의 `example.invalid`는 실제 원문 링크가 아니며 `original_location`은 가상 문단 위치다. 날짜 미상 사본을 최신 버전으로 간주해서는 안 된다.

이 자료는 격리된 테스트 인덱스나 메모리 기반 테스트에만 사용한다. 운영 Elasticsearch에는 자동으로 넣지 않는다. 실제 gold와 가상 gold의 평균 지표를 섞지 않는다.

## 측정과 답변 판정

검색은 문서 Recall/nDCG, 정확한 근거 청크 Recall, 지연을 측정한다. 답 없는 질문에서 검색 결과가 나오는 현상은 검색 오탐으로 따로 기록하며, 결과 존재만으로 답변 가능 여부를 결정하지 않는다.

답변은 저장한 실제 검색 컨텍스트를 읽은 소비자의 주장·인용을 저장한 뒤 독립적으로 판정한다. 각 인용이 전달된 문단에 실제로 존재하는지 확인하고, 별도 검토자가 각 주장의 정답 여부·충분한 지지·해당 인용의 지지를 평가한다. 검토 결과는 정확한 답변·컨텍스트 해시에 묶는다. 정확도, Citation precision, 인용 누락, 근거 부족에서의 보류, 상충 자료의 양쪽 설명을 검색 성능과 따로 보고한다.

설정 간 같은 질문의 답변을 비교해 검색 근거 변화와 답변 변화가 함께 나타나는지 확인한다. 모델 실행·독립 판정이 없는 경우 답변 지표는 미측정으로 남긴다. 작은 수동 gold의 개선을 전체 운영 성능으로 일반화하지 않는다.

## 답변 제출 형식과 지표 정의

검색 실행 시 `--contexts-output data/evaluation/contexts.jsonl`을 추가하면 정답 라벨이 없는
문맥을 함께 저장한다. 소비자는 각 행의 `context.question`과 `context.rendered`를 읽고,
`context.evidence`의 실제 보이는 원문만 인용한다. `expected_claims`와 gold는 소비자에게
주지 않는다. 주장 하나에 여러 문서가 필요하면 해당 인용들을 같은 주장에 붙인다.

제출은 JSONL 한 행에 `mode`, `query_id`, `context_sha256`, `answer`, `review`를 둔다.
선택적인 `generator`에 모델/버전/실행 조건을 기록한다. 아래는 형식 예시이며 실제 판정이 아니다.

```json
{
  "mode": "baseline", "query_id": "fictional-example", "context_sha256": "saved-context-hash",
  "generator": {"model": "record-exact-model", "run": "record-run-id"},
  "answer": {
    "status": "answer", "reason": "",
    "claims": [{"id": "c1", "text": "가상 규정은 30일을 정한다.", "citations": [
      {"doc_id": "fixture/old", "chunk_index": 0, "quote": "통지 기간은 30일이다."}
    ]}]
  },
  "review": {
    "reviewer": "separate-reviewer-id", "answer_sha256": "exact-answer-hash",
    "context_sha256": "saved-context-hash", "context_sufficient": true, "note": "판정 이유",
    "claims": [{"id": "c1", "correct": true, "covers": ["gold-claim-id"],
      "fully_supported": true, "supported_citations": [0]}]
  }
}
```

해시는 `pkb.answer_eval.digest(value)`로 계산한다. `supported_citations`는 해당 주장 인용
목록의 **0부터 시작하는** 위치다. `correct`는 정답성, `covers`는 충족한 gold 주장 ID,
`fully_supported`는 주장 전체가 인용으로 뒷받침되는지에 대한 별도 판정이다.
`context_sufficient`는 전달된 문맥 전체로 질문에 충분히 답할 수 있는지에 대한 검토다.
gold와 다른 출처도 같은 사실을 뒷받침하면 충분한 문맥으로 인정한다. 생략하면 보류 지표는
미측정으로 남는다. 정확한 gold 문단 포함 여부는 `gold_evidence_complete`로 별도 기록한다. 없는/잘린
문구의 인용은 판정자가 지지한다고 써도 자동 검사에서 탈락한다. `status=abstain`에는
빈 `claims`와 비어 있지 않은 `reason`이 필요하다. `reason`은 보류 사유만 담으며,
사실 주장은 모두 `claims`로 분리해 검토해야 한다. 상충 자료를 설명한 답변은 `conflict`를 쓴다.

- **Answer accuracy**: 기대한 답변/상충/보류 상태이며, 모든 기대 주장을 맞추고 틀린 추가
  주장이 없는 질문 비율. 정답이지만 무인용인 답변은 이 지표와 인용 지표에서 다르게 평가된다.
- **Claim precision/recall**: 모든 제출 주장 중 맞는 비율 / 기대 주장 중 충족한 비율.
- **Citation precision**: 실제 전달된 원문의 정확한 인용이며 검토자가 주장을 지지한다고
  판단한 인용 수 / 모든 제출 인용 수. 미검토·분모 0은 1이 아닌 미측정(null)이다.
- **Citation completeness**: 전체 주장을 뒷받침하는 유효 인용을 갖춘 주장 수 / 제출 주장 수.
  일부 근거만 인용하면 precision은 높아도 completeness는 낮을 수 있다.
- **Unsupported claim rate**: 1 - Citation completeness. 인용 자체가 없는 주장도 포함한다.
- **Abstention when insufficient**: 검토자가 문맥이 불충분하다고 판정한 질문에서 보류한 비율.
  코퍼스에는 답이 있지만 검색이 놓친 경우도 포함한다. 충분한 자료에서 보류한 비율과
  no-answer 질문의 보류 비율은 별도로 기록한다. 특정 gold 문단 누락만으로 불충분을 단정하지 않는다.

보고서의 `paired_comparisons`는 동일 질문에 두 설정 모두 검토된 답변만 짝지어 계산한다.
추가/누락된 청크, 같은 청크 내 실제 보이는 문구 변화, 검색 지표 차이와 답변 정확도 차이를
함께 남긴다. 다른 질문 부분집합끼리 평균을 비교하지 않는다. 한 번의 소비자 실행은 모델
출력 변동성을 분리하지 못하므로 인과 효과나 전체 코퍼스 성능으로 단정하지 않는다.

설정 간 차이를 주장하려면 같은 문맥으로 답변을 여러 번 생성하고, 생성 모델과 다른 모델의
검토를 함께 받는다. 표본·검토자 조합마다 별도의 `--answers` 파일로 같은 검색 보고서를 재평가한다.
검토자 간 판정이 갈린 답변은 사람이 확인할 목록으로 남기고, 설정 차이가 표본 간 변동이나
검토 불일치보다 클 때만 검색 변경의 효과로 보고한다.
