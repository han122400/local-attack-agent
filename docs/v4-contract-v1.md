# v4 A1·A2 구현 계약 v1

현재 구현은 설정 준비·결과 보존·트리 저장/재생까지다. 실제 scorer, 탐욕 탐색 루프, 취약점 검증기는 A3·A4에서 연결한다. v4 준비 상태의 `execution_ready`는 항상 false이며 실제 탐색 시작 요청은 501로 거절한다.

## 설정 준비 API

`POST /api/v4/prepare`에 아래 JSON을 보내면 201과 `prepared: true`, `started: false`, `run_id`가 반환된다. 모델 목록·추론 API·대상 사이트·Docker·브라우저에 접속하지 않는다. 제공 모델은 문법만 검사하며 `scorer_validation=syntax_only`로 기록한다.

```json
{
  "experiment_mode": "v4",
  "target": "http://127.0.0.1:3000/",
  "goal": "SQLi 후보를 조사할 실험 준비",
  "model": "qwen3.5:2b",
  "scorer_provider": "openrouter",
  "scorer_model": "typesafe/jev-1.14",
  "scorer_settings": {}
}
```

`scorer_provider`는 `openrouter` 또는 `local`이다. 별도 `scorer_settings`에는 `rubric_version`, `reevaluation_policy=scoring_fields_v1`, `timeout_seconds`, `max_retries`, `max_calls`, `max_calls_per_parent`, `batch_size`, `max_input_bytes`를 선택적으로 저장할 수 있다. 로컬에 한해 `base_url`, `protocol=ollama|decisions`, `num_ctx`, `num_predict`, `temperature`를 받는다. 범위는 `experiment_config.py`에서 검증한다. 자격증명이나 OpenRouter 생성 옵션은 받지 않는다. 아직 결정하지 않은 판정 설정의 기본값을 임의로 채우지 않는다.

실행 LLM의 `model_settings`, 기존 실행 `budgets`도 따로 저장할 수 있다. 이는 미래 v4 실행에 필요한 모든 예산이 확정됐다는 뜻이 아니다. A3 첫 요청 전 계약 값과 첫 대상 실행 전 v4 예산 설정은 여전히 선행조건이다. v4 실행 프롬프트는 아직 없으므로 준비 요청에서 `prompt_version`을 생략한다. v3의 `/api/start`는 기존 프롬프트·실행 경로를 유지하고 `experiment_mode` 생략 시 v3로 기록한다. v3에서는 전달받은 scorer 설정을 사용하거나 저장하지 않는다.

준비 기록은 runtime·관리 프로세스가 없고 보존이 끝난 경우에만 다음 실험을 막지 않는다. `GET /api/runs/{run_id}`로 설정과 준비 상태를 읽을 수 있다. 기존 복구 API는 v4 준비 기록에 대해서 트리만 재구성한다. 준비 기록에 예상하지 못한 runtime·프로세스가 있으면 제거하지 않고 복구를 거절한다.

## 발견·결과 계약

`v4_contracts.py`의 `RESULT_SCHEMA`, `DISCOVERY_SCHEMA`가 JSON 구조를 제공하고, `validate_result`·`validate_discovery`가 필드·크기·URL·경로·시도 일치를 검증한다. 생성 모델이 노드 ID·부모 ID·위험도·확정 상태를 쓰면 거절한다.

- 발견: `kind=surface|candidate`, `relation=discovered|refines|enabled_by`, `locator`, `hypothesis`.
- locator: 절대 HTTP(S) `url`, HTTP `method`, 입력 이름 `input`, 구분 가능한 인증 맥락 이름 `auth_context`. 선택적으로 절대 경로 형태의 `route_template`. 실제 쿠키·비밀번호를 인증 맥락 이름으로 사용하지 않는다.
- candidate의 hypothesis: `type`(예: `sqli`)과 구체 가설 `statement`. surface의 hypothesis는 null.
- 중복 키: 노드 종류, 정규화 URL 또는 제공된 라우트 템플릿, 메서드, 입력, 인증 맥락, 가설 유형. 첫 발견 부모를 유지하고 다른 경로는 `discovery_paths`에 기록한다. 이 보조 링크는 탐색 자식 관계에 추가하지 않는다.
- 안정 ID: 하네스가 노드를 `N0001…`, 시도를 `A0001…` 순서로 발급한다. 노드 삭제·ID 재사용은 지원하지 않는다.

하네스는 결과를 만드는 실행 전에 `TreeStore.begin_attempt(node_id, call_id=...)`를 호출하고 반환된 `result_path`를 모델에 전달한다. 결과 경로가 미리 존재하면 새 시도에 재사용하지 않는다. 결과 예시는 다음과 같다.

```json
{
  "schema_version": 1,
  "attempt_id": "A0001",
  "node_id": "N0001",
  "discovered": [
    {
      "kind": "candidate",
      "relation": "discovered",
      "locator": {
        "url": "http://127.0.0.1:3000/search",
        "method": "GET",
        "input": "q",
        "auth_context": "anonymous"
      },
      "hypothesis": {"type": "sqli", "statement": "검색 입력에 따른 질의 동작 차이 후보"}
    }
  ],
  "proposed_verdict": "inconclusive",
  "claim": "정찰에서 발견한 후보이며 검증 전",
  "evidence_refs": [{"path": "work/observations/response.json", "description": "관찰 파일"}],
  "next_observation": "기준 요청과 비교"
}
```

`proposed_verdict=confirmed|rejected|inconclusive`는 모델의 주장이다. A1·A2에서는 모두 최종 `verification_state=inconclusive`로 반영된다. 최초 미시도 노드는 `untested`이며 위험도는 `pending`(루트는 `not_applicable`)이다.

## 즉시 보존과 실행 연결

결과는 `runtime/work/node-results/<attempt_id>.json`에 쓴다. `TreeStore.import_result(attempt_id, exec_id=...)`는 같은 시도 이후에 기록된 `exec_started`와 동일 call/exec ID·action의 `exec_finished`를 확인한다. 종료 코드·취소/시간 초과 여부·실패 출처 필드의 존재와 타입도 검증한다. 다른 실행의 결과나 필드가 누락된 기록을 성공 근거로 사용할 수 없다.

결과 JSON은 최대 1 MiB, 발견은 최대 100개, 증거 참조는 최대 50개다. 증거는 `work/` 또는 `tools/` 아래 일반 파일만 받으며 파일당 10 MiB·합계 50 MiB로 제한한다. 경로 이탈·심볼릭 링크·Windows reparse point·잘못된 JSON·중복 필드를 거절한다.

순서는 **파일 읽기/검증 → 결과와 증거를 불변 artifacts로 보존 → `node_result_imported` 이벤트 append/fsync → reducer 적용 → 스냅샷 교체**다. 이벤트의 최종 증거 참조는 `artifacts/` 경로·크기·SHA-256과 모델 작성 출처를 담는다. 원래 모델 JSON은 별도 artifact로 남기되, 그 내부 runtime 참조를 영속 증거 참조로 사용하지 않는다.

동일 시도의 재흡수는 기존 결과를 반환하며 새 노드를 만들지 않는다. 실패한 실행 결과는 보존하지만 발견 노드를 추가하지 않는다. 성공 종료 이벤트가 없는 시도는 일반 가져오기로 처리하지 않는다. `interrupted=True` 복구 경로는 호출자가 먼저 실행·파일 쓰기를 중지한 뒤 사용해야 하며, 결과는 중단된 미확정 관찰로만 보존한다. 이 중지/미흡수 파일 탐색의 최종 정리 연결은 A4에서 구현한다.

## 이벤트와 재생

트리 이벤트는 공통 `run_id`, 연속 `seq`, `kind`, `at` 및 `tree_schema=1`, `payload`를 사용한다. 현재 종류는 다음과 같다.

| 이벤트 | payload / 실행 식별 |
| --- | --- |
| `tree_initialized` | `target` → 루트 N0001 생성 |
| `node_discovered` | `parent_id`, 검증한 `discovery` → 중복 키로 병합 또는 순번 ID 생성 |
| `node_selected` | `node_id` → 현재 위치·active 상태 저장; 선택 알고리즘은 아직 없음 |
| `node_state_changed` | `node_id`, `state=done|blocked`; 미완료 자식 또는 미흡수 시도가 있으면 done 불가. 종료 노드에는 새 발견을 추가하지 않음 |
| `node_attempt_started` | `attempt_id`, `node_id`; 상위 `call_id` |
| `node_result_imported` | `attempt_id`, `execution_status`, 모델 결과 필드, `result_ref`, 영속 `evidence_refs`; 상위 `call_id`, `exec_id` |

`replay_tree(events, run_id)`는 순수 reducer다. 외부 실행을 재발행하지 않으며, 다른 run·순서 누락·지원하지 않는 트리 버전/이벤트·충돌하는 중복 결과를 거절한다. `TreeStore.rebuild()`는 전체 이벤트를 검증하고 보존 증거의 해시를 확인한 뒤 `tree.json`·`nodes/N….json`을 원자적으로 다시 쓴다. 각 노드 스냅샷과 트리 색인에는 마지막 적용 이벤트 번호가 있다. 노드들을 먼저, 색인을 마지막에 교체하며 파일 집합 전체를 하나의 트랜잭션으로 간주하지 않는다.

스냅샷 저장 실패 뒤에도 이미 기록한 이벤트가 정본이다. 재생으로 스냅샷을 복구하기 전까지 해당 writer의 추가 트리 변경을 막는다. 원본 결과·runtime 삭제는 이 계층에서 수행하지 않는다. A2 준비 상태의 설정을 담은 `v4_preparation_created` 이벤트 덕분에 session.json이 사라져도 v4 설정을 복원한다. v4 이벤트는 있는데 설정 근거가 없으면 v3 복구로 우회하지 않고 실패한다.

## 구현 범위와 다음 연결

- A1·A2: 계약 검증, 설정 준비 API, 이벤트/스냅샷, 즉시 결과 보존, 중복 방지, 준비 상태 복구.
- A3: 실제 모델 목록/추론 계약 확인, 점수·배치·캐시·예산 집행.
- A4: 결과 계약을 실행 프롬프트/루프에 전달, 시도와 명령 연결, 실행 중지 후 미흡수 결과 처리, SQLi 검증기와 보고/축약 보고.
- B: 로컬 실호환성, 대시보드 설정, 트리 조회 화면·export 확장.

API/저장 계층 단위 검증은 브라우저·실제 추론·대상 실험 없이 수행한다. 이 문서의 예시는 실행 기록이나 검증 성공 증거가 아니다.
