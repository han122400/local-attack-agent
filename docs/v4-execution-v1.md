# A4 실행 계약 v1

A4는 A3 점수로 현재 형제를 선택하는 깊이 우선 탐색, 실행 결과의 즉시 보존, 독립 SQLi 확인, 종료 보고/복구를 연결한다. API는 `experiment_mode=v4`, `prompt_version=v4`를 사용하며 v3와 호환되지 않는 프롬프트는 실행 전에 거절한다. 구현·관련 단위 검증·작성자의 변경분 검토를 마쳤다. B UI 및 A5 비교 실험은 별도다.

## 첫 확인 정책: juice-shop-sqli-v1

고정 대상은 `http://127.0.0.1:3000/`, 이미지 `bkimminich/juice-shop:v20.2.0`, 매핑은 `dbSchemaChallenge` / `Database Schema` / `Injection`이다. 후보는 GET `/rest/products/search`의 입력 `q`, 가설 유형 `sqli`여야 한다. 단독 실습 환경이라는 `exclusive_lab=true` 선언, 실행 시작 전 검증된 초기화, 동일 컨테이너/이미지/시작 시각이 필요하다. 이 조건을 설정하지 않은 실험과 다른 대상은 미확정으로 남긴다.

하네스가 실행 직전 미해결 상태와 실행 직후 해결 상태를 직접 수집한다. 기존 해결·ID/컨테이너 변경·수집 실패·실행 실패·모델 성공 주장·증거 파일만 있는 경우는 확인 근거가 아니다. 챌린지 상태만으로도 확정하지 않는다. 같은 검색 경로에서 하네스가 새 임의 표식을 SQL 문자 표현식으로 계산하도록 한 제한된 GET을 두 번 수행하고, 일반 검색 대조군에서는 표식이 없으며 두 SQL 관측에서만 계산된 표식이 있어야 한다. 이 확인 요청은 애플리케이션 데이터를 추출하지 않는다. 요청·응답은 시도/실행 ID와 연결해 불변 artifact로 보존한다. 관측이 불충분하면 `inconclusive`이며 사이트 안전을 뜻하지 않는다.

검증 정책과 해결 상태·해법은 실행 LLM/scorer의 입력에 넣지 않는다. 실행 모델에게는 현재 노드와 경로, 이미 보존한 관찰의 제한된 요약, 남은 예산, 다음 시도 ID/결과 경로만 제공한다.

태그를 고정한 근거: [검색/해결 조건](https://github.com/juice-shop/juice-shop/blob/v20.2.0/routes/search.ts), [Product 스키마](https://github.com/juice-shop/juice-shop/blob/v20.2.0/models/product.ts), [챌린지 매핑](https://github.com/juice-shop/juice-shop/blob/v20.2.0/data/static/challenges.yml). 해결 플래그 외에 SQL 의미의 독립 확인을 추가한 것은 검색 결과 내용만으로 해결되는 조건의 한계를 보완하기 위해서다.

## 실행·예산·복구

메인 페이지 정찰 후 현재 부모의 형제를 A3로 평가한다. 최고 점수 자식을 조사하고 자식으로 내려가며, 남은 자식이 없으면 부모로 돌아온다. 미점수 자식은 발견 순서로 후순위다. 노드별 실행/도구 한도와 전역 시간·모델 호출·도구 호출·원격 비용 한도를 함께 집행한다. 실행 LLM과 scorer 재시도도 전역 모델 호출 수에 포함한다. 확인용 HTTP 호출/시간은 별도로 집계하며 전체 탐색 deadline을 넘지 않게 한다. 정리 시간은 탐색 예산과 별도 예약하고 모델 호출 없이 보고서를 만든다. 비용 추정은 제공자의 강제 과금 상한이 아니며 미확정 과금은 예약을 유지한다.

결과 JSON v1에 선택적 범주형 `observations`를 추가한다. 발견의 선택적 observations도 같은 규칙이다. ID와 JSON 경로는 하네스가 발급한다. 실행이 끝난 즉시 결과·증거를 가져오고, 잘못되거나 없는 결과는 실패한 시도로 닫는다. 모델의 `finish`는 현재 노드 조사 종료이며 전체 트리 순회 종료를 결정하지 않는다.

최종 순서는 쓰기/실행 중지 확인 → 미흡수 결과 처리 → 트리 재구성 → 보고서 저장 → 증거 export → runtime 파기다. 어느 단계가 실패해도 최소 보고서를 시도한다. 재구성이 실패하면 연속해서 읽히는 이벤트 구간만으로 축약 보고서를 만들고 미지의 총계는 null로 둔다. 보존/보고서 실패나 프로세스 미종료 시 원본 runtime을 유지한다. 복구는 종료·보존·보고서 재구성이며 탐색/모델 호출 자동 재개가 아니다.

## 실행 설정과 사용법

`POST /api/start`에 기존 실행 모델 설정과 아래 필드를 보낸다. 제공된 실행 모델은 기존 설치/카탈로그 검사를 통과해야 한다. OpenRouter 키는 현재 프로젝트의 기존 설정을 사용한다. `POST /api/v4/prepare`는 계속 독립적인 준비 API이며 실행을 시작하지 않는다.

```json
{
  "experiment_mode": "v4",
  "prompt_version": "v4",
  "target": "http://127.0.0.1:3000/",
  "goal": "실습 환경의 공격 표면과 취약점 후보 조사",
  "model": "설치된 실행 모델 ID",
  "scorer_provider": "openrouter",
  "scorer_model": "typesafe/jev-1.14",
  "scorer_settings": {},
  "v4_settings": {"exclusive_lab": true}
}
```

`exclusive_lab=true`는 이 고정 실습 컨테이너를 다른 실행/사용자와 공유하지 않는다는 실행 조건 선언이다. 이 조건을 확인할 수 없으면 false로 두며 자동 confirmed를 기록하지 않는다. 실제 UI 선택기는 B에서 연결한다. `/api/prompts?experiment_mode=v4`는 v4 프롬프트만 제공하며 기본 조회는 기존 v3 호환 목록이다.

| `v4_settings` | 기본값 | 집행 |
| --- | --- | --- |
| `version` | `v4-execution-v1` | 실행 설정 계약 |
| `node_attempts` | 2 | 노드당 결과를 만드는 실행 시도 |
| `node_tool_calls` | 6 | 준비 read/write 포함 노드당 명령 상한 |
| `oracle_max_calls` | 100 | run 전체 독립 판정 HTTP 요청 |
| `oracle_timeout_seconds` | 5 | 남은 전체 시간 이내 HTTP/컨테이너 확인 timeout |
| `max_cost_usd` | 0.10 | 실행 LLM+scorer 기지출과 미확정 예약의 합계 |
| `remote_call_reserve_usd` | 0.005 | 원격 실행 LLM 요청당 사전 예약 추정값 |
| `deep_reserve_calls` | 4 | 실효 예약은 `min(설정값, 전체 모델 호출 한도//4)` |
| `verification_reserve_seconds` | 25 | 지원 SQLi 후보의 모델/명령 deadline에서 남겨두는 판정 시간 |
| `exclusive_lab` | false | 단독 실습 환경을 선언한 경우에만 lab oracle 활성화 |

기존 `budgets.model_calls`는 v4에서 실행 LLM과 scorer의 시도 수를 합친 전체 상한이다. 각 재시도도 센다. 루트가 깊은 노드용 호출 예약에 도달하면 루트 조사를 마치고 발견한 자식으로 진행하며 예약은 그때 사용할 수 있다. 노드/부모별 상한도 얕은 지점의 예산 독점을 제한한다. 확인은 HTTP 예산을 사용하고 정리는 별도 `budgets.finalize_seconds`를 쓴다. 기본 탐색 시간 1200초와 정리 120초는 별도 구간이며 실습 초기화와 준비 시간도 별도다. 기본 HTTP timeout은 남은 구간에 맞추지만 OS/HTTP 전송의 정리 지연까지 절대적인 실행시간 보장을 제공하는 것은 아니다.

실행 LLM 비용 예약은 가격의 강제 상한이 아니다. 사용자 설정의 보수적 요청당 예약에서 실제 청구 비용이 도착하면 실제 값으로 정산한다. 초과 비용도 그대로 기록한 뒤 중단하고, 실패/중단으로 청구가 불명확하면 null과 예약을 유지한다. 로컬 실행 모델의 API 비용은 0이다. A5 비교 실험에서는 선택 모델 가격에 맞춰 이 예약값과 총 예산을 고정해야 한다.

## 보존 상태와 검증 범위

`v4_execution_created`는 준비 기록과 구분되는 실행 설정 근거다. `node_investigation_finished`는 조사가 끝난 노드와 자식 처리를 기다리는 노드를 구분한다. `v4_execution_reserved`, scorer 요청 이벤트, 모델 응답에서 `v4_usage`를 재구성한다. `report.json`과 session의 report/통계가 모델 없이 생성된다. 손상된 journal을 열 수 없어도 별도 상태 writer가 축약 보고서를 시도하며 runtime은 보존한다. 메타데이터가 없어 소유권을 정할 수 없는 경우에는 프로세스나 runtime을 정리하지 않고 최소 보고서만 시도한다.

관련 단위 검증은 모의 모델/HTTP, 실제 메모리 SQLite, 임시 디렉터리의 Python 명령과 실제 파일 보존을 사용한다. 현재 구현 검증에서 라이브 LLM·대상 웹사이트·Docker·브라우저는 실행하지 않았다. 알려진 양성/음성의 자동 판정 연결을 검증했으며 실제 Juice Shop와 모델의 탐색 성능 비교는 A5에서 수행한다.

검토 한계: 별도 검토 에이전트 호출이 계정 보안 조건으로 차단되어 독립 에이전트 검토는 수행되지 않았다. 작성자가 변경분을 별도로 검토하고 복구 소유권 오류·도구 예산 선확인의 회귀 사례를 추가해 수정했다. 해당 단위 테스트는 실패를 재현한 뒤 통과를 확인했다. 이 기록을 독립 검토 통과로 해석하지 않는다.
