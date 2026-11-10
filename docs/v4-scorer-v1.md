# v4 A3 scorer 구현 계약

첫 OpenRouter 라이브 요청 전에 고정한 A3 계약이다. 아래 B6 확장은 별도 로컬 계약을 사용한다.

- 모델: `typesafe/jev-1.14`. 요청 직전 라이브 카탈로그의 정확한 ID·`decisions` 지원·canonical slug·가격·조회 시각을 보존한다. `latest`는 거절하고 다른 모델로 자동 전환하지 않는다.
- 엔드포인트: `POST https://openrouter.ai/api/alpha/decisions`. `questions.risk_<node_id>`는 `type=score`, `criteria`는 5개다. 질문 ID는 모델에게 보이지 않으므로 지시문에서 `nodes.<node_id>`를 명시한다.
- 기준 버전 `risk-priority-v1`: `risk_rubric.CRITERIA`의 영어 문구가 실제 전송 계약이다. 위험 오름차순으로 무시 가능(0), 낮음(1), 중간(2), 높음(3), 매우 높음(4). 영향과 관측된 실현 가능성을 함께 보는 순서형 기준이며 임의의 별도 영향/가능성 가중치는 만들지 않는다. 모델의 우선순위이며 취약점 확인 결과가 아니다.
- `scoring_fields_v1`: 노드 ID·종류, HTTP 메서드, 고정 경로 범주, 입력 범주, 인증 범주, 가설 유형 및 아래 관찰 필드만 전송한다. 호스트·실제 경로·쿼리·입력 이름·쿠키·헤더·본문·증거 경로·자유 서술은 보내지 않는다. 알 수 없는 값은 `unknown/other`, 미정 관찰은 기본값이다. 어댑터가 네트워크 직전에 다시 투영한다.
- 관찰: `input_control`(unknown/none/partial/full), `object_scope`(unknown/self/cross_user/public), `reflection`(unknown/none/escaped/unescaped), `data_exposure`(unknown/none/public/private/sensitive), `query_behavior`(unknown/unchanged/error/controlled_difference), `auth_required/upload_public`(bool/null), `baseline_compared/reproduced`(bool), `response_status`(100..599/null). 본문 등 미지원 필드는 버리고, 지원 필드의 잘못된 값은 입력 오류다. 관찰은 독립 검증 전까지 주장일 수 있다.
- 초기 `decisions-score-v1`은 확률 합 1±0.000001, 가중합 오차 0.005001로 요청 전에 고정했으나 실제 응답과 맞지 않았다. 아래 근거에 따라 새 `decisions-score-v2-rounded`를 사용한다. `score`는 유한한 0..4, 확률 키는 정확히 0..4, 각 확률 0..1이다. 각 확률·score에 ±0.005(두 자리 반올림) 구간을 만들고, **모든 구간을 동시에 만족하며 합이 1이고 가중합이 score 구간 안인 원래 분포가 존재**해야 한다. 0..1 경계도 유지한다. 단순 허용치 상한은 합 0.025001, 가중합 0.055001이지만 실제 판정은 더 엄격한 구간 동시 만족 검사다. 수치 비교 epsilon은 0.000001이다. `legend`는 순서/문구가 기준과 같아야 하고 `confidence`는 유한한 0..1이다. 정규화는 `100×score/4`. 확률을 재정규화하거나 신뢰도를 만들어 넣지 않으며, 결측 비용을 0으로 채우지 않는다.
- 응답의 실제 모델은 고정 ID 또는 카탈로그 canonical slug와 같아야 한다. `usage.input_tokens/output_tokens`는 음이 아닌 정수, `usage.cost`는 음이 아닌 유한한 USD다. 한 답변의 누락/오류는 노드 오류, 모델·usage·응답 봉투 오류는 제공자 계약 오류다.
- 라이브 연결 확인: 합성 노드 1개, POST 최대 1회(재시도 없음), 요청 16,384 bytes 이하, timeout 20초, 사전 비용 예약 상한 $0.001. 카탈로그 가격으로 `(요청 UTF-8 bytes+8192)×입력 단가 + 8192×출력 단가 + 요청 단가`를 예약한다. 이는 로컬의 보수적 추정이지 제공자 과금의 강제 상한이 아니다. 실제 비용을 기록하고 초과 시 실패로 기록한다.
- 응답 보존은 검증된 모델 ID·사용량·점수·원점수·확률·신뢰도로 제한한다. 임의 응답 본문·에러 텍스트·헤더는 보존하지 않는다. 요청은 전송한 범주형 JSON 그대로 보존한다. API 키는 환경에 주입하거나 로그에 기록하지 않는다.

공식 계약: [OpenRouter Decisions](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request), [TypeSafe Score](https://docs.typesafe.ai/primitives/score).

연결 이력: 첫 합성 POST는 모델/usage 계약을 통과했으나 노드 답변 검증에 실패했다($0.00003675). 최초 진단이 오류를 단일 코드로 축약해 원인을 보존하지 못했다. 수치 허용오차는 유지하고, 숫자 필드와 고정 오류 코드만 남기는 진단을 추가해 합성 요청 1건을 추가 확인한다. 각 명시적 probe는 POST 1회이며 자동 재시도하지 않는다.

두 번째 요청($0.00003675)에서 `score=1.85`, 확률 `{0:0,1:0.14,2:0.86,3:0,4:0}`를 관찰했다. 표시 확률의 가중합은 1.86이지만 독립적인 두 자리 반올림으로 설명 가능한 값이다. 제공자의 내부 계산을 확인한 것은 아니다. 사후에 v1을 통과시킨 것으로 바꾸지 않고 실패 기록을 유지하며, **명시적 두 자리 호환 프로필**을 갖는 v2를 추가했다. 위 구간 검사를 양성·음성 단위 테스트로 고정하고 세 번째 합성 POST로 다시 확인한다. 이후 정밀도가 달라지면 새 계약 버전으로 검토한다.

세 번째 요청은 v2를 통과했다. 2026-10-27 09:00 UTC, 실제 모델 `typesafe/jev-1.14-20261014`, 원점수 1.91 → 47.75/100, input 875 / output 22 tokens, $0.00003675. [실제 요청·검증 응답](evidence/a3-jev-1.14-contract.json)을 보존했다. 실패 진단을 포함한 3건 합계는 $0.00011025다. 원본 probe 기록은 `runs/scorer-contract-probes/`에 있으며 실험 성과·대상 요청 수에 포함하지 않는다. 이 확인은 위험도 순위 품질을 입증하지 않는다.

## 내부 API와 저장

`RiskScorer(tree, adapter=...)`의 `score_children(parent_id)`가 A4에서 사용할 진입점이다. 기본 어댑터는 OpenRouter다. 호출자가 단일 SessionLog writer/실행 lease를 소유해야 한다. 소유한 기본 HTTP 클라이언트는 `RiskScorer.close()`로 닫는다.

1. `TreeStore.discover`로 발견을 저장하고 `set_observations(node_id, facts)`로 범주형 관찰을 저장한다. 관찰 전체를 교체하므로 미지정 필드는 unknown/false/null로 돌아간다. 자유 텍스트 필드는 무시한다. A1 결과 JSON 스키마는 변경하지 않았다. 생성 결과를 이 관찰 스키마에 연결하는 작업은 A4다.
2. 자식 선택 시 `score_children(parent_id)`를 호출한다. `ready=true`이면 `ordered_node_ids`를 점수 내림차순 → 발견 순서 → ID로 반환한다. 점수가 없는 자식은 후순위이며 `unscored`에 사유, `fallback_node_ids`에 ID가 남는다. A3 자체는 노드를 선택하거나 명령을 실행하지 않는다.
3. `ready=false`이면 선택 목록은 빈 배열이다. `stop_reason`을 A4 종료/정리로 전달한다. `context_changed`는 요청 중 관련 관찰이 바뀌었다는 뜻이다. 현재 맥락을 다시 확인할 수 있으며, 응답은 이전 맥락의 이력으로만 보존한다.

첫 점수화 때 카탈로그를 조회하고 `scorer_configured`로 제공자·정확한 모델 ID/canonical slug/가격/조회 시각·실효 설정·계약 버전을 고정한다. 같은 run에서 이를 바꾸지 않는다. 원래 `scorer_settings`는 입력값 그대로 두고 `session.json.scorer_effective_settings/scorer_model_pin/scorer_usage`에 적용값·사용량을 저장한다. `scorer_validation=catalog_verified`는 목록 호환성만 뜻하며 매번 실제 응답 계약을 별도로 검증한다. 준비 API만 호출하면 syntax_only다.

영속 이벤트는 `node_observations_changed`, `scorer_configured`, `scorer_request_started`, `scorer_request_finished`, `scorer_batch_applied`, `scorer_halted`다. 요청 전 전송 JSON·입력 해시·맥락 키·후보 ID·비용 예약을 append/fsync한다. 종료 이벤트에는 검증된 응답 또는 고정 오류 코드·실제 사용량·소요 시간을 담는다. 형제 평가가 끝나면 노드 `risk`와 `nodes/`, `scorer.json`, `session.json`을 갱신한다. 위험도는 `verification_state`를 바꾸지 않는다.

재생은 점수·캐시·사용량·예약·종료 사유만 복원한다. 카탈로그/모델/대상 호출을 하지 않는다. 시작 기록만 있고 종료가 없으면 송신·과금 여부가 불명확하므로 예약을 유지하고 `scorer_unavailable`로 선택을 막는다. 자동 재시도하지 않는다. `requests`는 예산 집행을 위해 미확정 요청도 포함하며 `completed_requests/pending_requests`로 구분한다. 비용 미확정이 있으면 `cost_usd=null`, `known_cost_usd`와 `reserved_cost_usd`를 별도 표시한다.

## 배치·캐시·오류 규칙

- 모든 형제를 공유 맥락에 넣고 질문을 발견 순서로 나눈다. 완료된 형제도 맥락에 남기므로 방문·완료·복귀만으로 캐시가 깨지지 않는다. 형제 전체가 scored/unscored로 결정되기 전에는 선택 목록을 제공하지 않는다.
- 캐시 키는 제공자/모델/설정/계약, 부모와 모든 형제의 투영된 관찰, 로컬 locator 해시를 포함한다. 새 형제·관련 관찰 변경은 미완료 형제 점수를 무효화한다. 본문 첨부·시도 횟수만 바뀌는 경우는 유지한다. 과거 맥락으로 돌아오면 캐시와 노드 스냅샷을 복원한다.
- 질문 수를 줄여 요청 bytes 한도에 맞춘다. 질문 1개도 공유 맥락과 함께 들어가지 않으면 `input_limit`로 남긴다. 형제 정보를 임의로 잘라내거나 원문을 보내지 않는다.
- 개별 답변 오류는 실패 노드만 재시도하고 `unscored/invalid_answer`로 남긴다. 전부 개별 오류여도 제공자 전체 장애로 분류하지 않는다. 입력 오류는 `invalid_input`이다.
- 인증/모델/응답 봉투·압축 디코딩 오류는 제공자 실패다. 추론의 429/5xx/전송 오류만 제한적으로 재시도하며 이후 `scorer_unavailable`로 종료한다. 실패 응답도 종료 이벤트에 기록하며 비용이 없으면 미확정 예약을 유지한다. 과대 JSON 정수는 예외로 빠져나가지 않고 개별 답변/계약 오류로 정리한다. 모델 자동 대체는 없다.
- 부모 누적 한도는 맥락이 바뀌어도 초기화하지 않는다. 한도에 걸리면 남은 자식은 `parent_limit`다. 전체 호출·비용·시간 한도가 끝나면 부분 점수만으로 선택을 계속하지 않는다. 제공자 오류 재시도를 부모 한도로 끝내야 하면 제공자 장애로 종료한다.

## 시작 설정: scorer-settings-v1

| 설정 | 기본값 | 단위/규칙 |
| --- | --- | --- |
| `batch_size` | 8 | POST당 질문 상한 |
| `max_input_bytes` | 16,384 | 투영 후 전체 JSON의 UTF-8 bytes |
| `max_calls` | 20 | 재시도·미확정 송신 예약 포함 |
| `max_calls_per_parent` | 4 | 부모 누적 요청; 전체 상한보다 크면 기본값만 축소 |
| `max_retries` | 1 | 노드/동일 맥락 또는 연속된 일시적 제공자 실패의 추가 시도 |
| `timeout_seconds` | 20 | HTTP 전송 timeout; 남은 scorer 시간 이내로 설정 |
| `time_budget_seconds` | 120 | 카탈로그 및 모든 추론 시도의 누적 시간 |
| `max_cost_usd` | 0.01 | 기지출+미확정 예약+다음 요청 예약 초과 시 중단 |

성능을 입증한 값이 아니라 초기 연구 설정이다. 부모 1개가 기본 전체 20회 중 최대 4회를 쓴다. 카탈로그 조회 timeout도 scorer 시간 예산 이내로 제한하며, 조회 실패 시간·오류 코드는 종료 이벤트에 보존한다. 비용 예약은 제공자 과금의 강제 상한이 아니므로 초과 비용도 그대로 기록하고 중단한다. A4의 실행 LLM·명령·검증·정리 예산 예약 및 총 wall-clock 한도는 별도로 적용된다.

## B6 로컬 Ollama 확장

- 로컬 모델은 `GET /api/tags`의 정확한 이름과 64자리 SHA-256 digest로 고정한다. 각 판정 전에 digest를 다시 조회해 이름이 다른 모델을 가리키면 중단한다. API 주소는 루프백 HTTP의 루트 경로만 허용한다.
- `POST /api/chat`에 JSON schema, `stream=false`, `think=false`를 전달한다. 외부 Decisions 경로와 동일한 범주형 노드 투영 및 위험 오름차순 5단계 문구만 입력으로 사용한다. 모델은 노드마다 정수 등급 0~4를 반환한다.
- 로컬 계약 `ordinal-label-v1`은 `score=100×label/4`, `score_method=ordinal_label`이며 `probabilities`와 `confidence`는 `null`이다. 토큰 수는 Ollama의 `prompt_eval_count`, `eval_count`를 보존하고 API 비용은 0으로 기록한다. v4 이벤트 재생은 제공자별 계약으로 검증한다.
- `scorer_settings` 공통 예산과 별도로 `protocol=ollama`, `base_url`, `num_ctx`, `num_predict`, `temperature`를 저장한다. OpenRouter 기존 `decisions-score-v2-rounded` 이벤트는 그대로 재생한다. 목록 조회 API는 `/api/scorer/models`이며 실행 LLM 목록과 독립적이다.
- `max_input_bytes`는 로컬 경로에서 실제 `/api/chat` JSON 전체의 UTF-8 크기로 계산한다. 기록용 범주형 요청만 한도 내이고 전송 JSON은 초과하는 경우, 송신 전에 해당 노드를 `input_limit`로 남긴다.

## 연결 확인 CLI

`python -m tools.check_v4_scorer`는 합성 JSON만 출력한다. `--live`를 명시해야 POST 1회를 수행한다. `--env-file <프로젝트 .env 경로>`로 기존 키를 메모리에서만 읽을 수 있다. 키 내용은 명령 인자로 넘기지 않는다. 각 호출은 별도 proof를 만들고 기존 성공 기록을 덮어쓰지 않는다. A4에서 실제 `/api/start` 연결을 추가했으며 사용법은 [실행 계약](v4-execution-v1.md)을 따른다.
