# v4 실행 계약 v2

2026-10-30부터 v4 실행에는 Juice Shop 전용 자동 취약점 판정기가 없다. `v4-execution-v2` 설정은 노드 시도 횟수, 도구 호출, 깊은 노드용 호출 예약, 비용 한도만 다룬다. 이전의 `exclusive_lab`, `oracle_max_calls`, `oracle_timeout_seconds`, `verification_reserve_seconds`는 새 실행 설정에서 거절한다.

후보 노드도 다른 노드처럼 허가된 대상 origin 안에서 조사한다. 실행 모델이 작성한 명령, 결과 JSON, 원본 증거, 발견 노드와 시도 귀속은 기존 하네스가 보존한다. `proposed_verdict`는 모델의 주장으로만 기록하며 자동 `confirmed`나 `rejected`로 승격하지 않는다. 새 보고서의 `stats.confirmed`와 `stats.rejected`는 `null`이고, `tested_candidates`는 실행 시도가 있는 후보 수다. 이는 성공한 공격 수가 아니다.

2026-11-02부터 결과 JSON의 `next_observation`이 비어 있지 않으면 같은 노드를 노드별 시도·도구 한도 안에서 계속 조사한다. 다음 턴의 `previous_result`에는 직전 정상 결과의 요약과 런타임에서 읽을 수 있는 원본 결과·증거 파일 경로가 제공된다. 맥락 한도 때문에 요약을 줄인 경우 `details_truncated`로 표시하며, `result_path`에서 전체 이전 결과를 읽을 수 있다. 하위 노드에 대한 조사는 발견 노드로 넘기며, `next_observation`은 현재 노드에 남은 작업을 뜻한다. 후속 작업을 남긴 채 `finish`로 완료할 수 없다.

`v4-budget-v3`에서는 `node_attempts`가 연속 오류·무진전 결과 한도이고, `node_tool_calls`가 마지막 진전 이후 도구 호출 한도다. 새 증거 내용, 처음 관측한 구조화된 사실, 범위 안의 새 노드를 확보하면 두 카운터를 초기화한다. 정상적으로 진전하는 후속 조사는 총 시도 횟수 2회나 총 도구 호출 6회 때문에 종료하지 않으며, 전역 시간·호출·비용 예산을 사용한다. 파일명·시도 ID·후속 작업 문구만 바꾸거나 같은 응답을 다시 기록하는 것은 진전으로 보지 않는다. 하위 노드용 모델 호출 예약도 실제 미탐색 하위 노드가 있을 때 적용한다. 이 정책은 탐색 진행을 측정하며 취약점 성공을 판정하지 않는다.

연속 오류·무진전 한도 도달, 정상 관측 없이 종료, 루트에서 조사할 표면을 하나도 찾지 못한 경우에는 노드의 `investigation_state=incomplete`와 사유를 기록한다. 발견한 다른 가지의 조사는 계속한다. 트리 순회를 마쳤더라도 미완료 노드가 있으면 `run_result=incomplete`, `stop_reason=investigation_incomplete`로 종료한다. 전역 시간·호출·비용 한도에 의한 종료 사유는 해당 한도로 표시한다. 보고서의 `incomplete_nodes`에는 노드별 사유와 남은 후속 관측을 보존한다. 보고서 `status=complete`는 집계 자료를 정상 복구했다는 뜻이며, 조사 결과는 `run_result`로 판단한다.

과거 `node_verification_recorded` 이벤트는 로그 재생을 위해 읽는다. 당시의 증거 참조는 보존하되 제거된 판정 규칙을 다시 실행하지 않으며, 재생된 노드에는 `legacy_lab_verdict_unavailable`을 표시한다. 과거에 저장된 `report.json`은 역사적 산출물로 유지한다. Juice Shop 컨테이너 초기화와 A5의 사례별 챌린지 상태 수집은 탐색기의 취약점 판정 기능과 별개다.
