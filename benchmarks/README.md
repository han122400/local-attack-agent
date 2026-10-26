# 조사 정책 예비 비교

`harness_policy_eval.py`는 운영 코드를 바꾸지 않고 두 정책을 따로 비교합니다.

- `original`: `finish`로 전체 실행 종료.
- `stall3`: 완료 단계에 도달하지 못한 실행이 세 번이면 다음 항목으로 이동.
- `case_finish`: `finish`로 현재 항목만 정리하고 다음 항목으로 이동.
- `combined`: 두 변경을 함께 적용.

실제 설치된 Ollama 모델과 기존 `Harness`를 사용하되, 모델이 반환한 `read_file`과 `finish`만
메모리 속 고정 문제에 적용합니다. 사이트 접속, HTTP 증거 수집, 브라우저 실행, 생성 코드
실행은 하지 않습니다. 모델의 완료 주장이나 stdout이 아니라, 정해진 파일 참조를 실제로 모두
읽었는지와 최종 종료 여부로 점수를 냅니다. `success`는 이 제한된 작업 흐름의 성공이며
취약점 판정이나 최종 설명의 정확도를 뜻하지 않습니다.

기본값은 앱과 같은 컨텍스트 8192·출력 4096과 Harness의 기존 재시도를 사용합니다. 먼저
`original`만 측정해 명령 생성이 동작하는지 확인합니다. 한 모델만 순차 호출합니다.

```bash
.venv/bin/python benchmarks/harness_policy_eval.py \
  --model hf.co/fdtn-ai/Foundation-Sec-8B-Reasoning-Q4_K_M-GGUF:Q4_K_M \
  --cases two_cases --policies original \
  --max-calls 4 --seconds-per-run 180 \
  --seed 0 --output /tmp/harness-command-profile.json
```

`--policies original,stall3,case_finish,combined`와 `--cases four_step_setup,two_cases,blocked_then_easy`로
조합을 늘릴 수 있습니다. 본실험은 같은 문제·설정에서 seed를 바꿔 반복해야 합니다.
모델 생성 오류·출력 잘림·시간 초과는 `model_failure`로 분리하고
`eligible_for_policy_comparison=false`로 표시합니다. 이런 행을 정책의
탐지 정확도 비교에 사용하면 안 됩니다. 네트워크 응답을 기다리는 시간은 모델 호출의 자체
연결/읽기 제한에 따라 `seconds-per-run`을 초과할 수 있습니다.

종료 정책의 전후 비교는 동일한 설정으로 `--policies original,case_finish` 등을 지정합니다.
운영의 기본 조사 순서나 `finish` 의미는 바뀌지 않습니다. 512토큰·재시도 1회로 제한한 이전
예비 결과는 운영 설정의 성능으로 일반화하지 않습니다.

핵심 지표는 `completed_tasks`, `missed_solvable_tasks`, `provider_calls`, `elapsed_seconds`입니다.
`valid_commands`와 `valid_command_rate`는 스키마·입력 검증을 통과한 명령 수/모델 시도 수이며,
명령 내용의 정확성이나 취약점 판정 정확도가 아닙니다. 명령 생성률이 확보된 뒤 완료율을 비교합니다.
`mean_time_to_content_seconds`는 호출부터 첫 답변 문자가 나올 때까지, `mean_command_seconds`는
검증된 명령이 나올 때까지 걸린 시간입니다. `thinking_chars`와 `content_chars`는 문자 수입니다.
추론 토큰과 답변 토큰을 정확히 나눈 값이 아닙니다. `output_limit_count`는 출력 잘림 횟수,
`completed_generation_tps`는 완료 응답의 토큰 생성 속도, `load_seconds`/`prompt_eval_seconds`는
모델 로드/입력 처리 시간입니다. 토큰 수나 시간이 보고되지 않거나 스트림이 미완료이면 해당 합계는
null입니다. 시스템 프롬프트 해시도 저장합니다. 기존 `scorecard.py` 점수는 사용하지 않습니다.
`four_step_setup`은 중간 파일 읽기도 유효한 진전인데 단계 완료만 세면 세 번째에 포기하는
반례입니다. 이를 실제 사이트에서의 성능 개선/악화 비율로 일반화할 수 없습니다.
