# v4 B6–B8 구현 기록

2026-10-29. A1–A5 저장·탐색·검증 경로 위에 판정 제공자와 대시보드/조회 기능을 연결했다.

## 구현

- **B6 · 판정 제공자:** OpenRouter Decisions 모델은 라이브 카탈로그의 `decisions` 형식·가격·정확한 버전 ID로 필터링한다. 로컬 Ollama는 `/api/tags`의 이름/digest를 고정하고 `/api/chat`의 JSON schema로 형제별 0~4 등급을 받는다. 로컬 점수의 `score_method=ordinal_label`, 확률·신뢰도 `null`, API 비용 0을 보존한다. 실제 전송 JSON 전체에 `max_input_bytes`를 적용한다. [Ollama chat API](https://docs.ollama.com/api/chat), [모델 목록 API](https://docs.ollama.com/api/tags).
- **B7 · 대시보드:** 실행 LLM 선택을 1단계로 표시한다. 실험 방식 v3/v4를 선택하고, v4에서는 2단계 제공자(OpenRouter/로컬), 판정 모델 검색·선택, 공통 호출·시간·비용 한도와 로컬 옵션을 설정한다. 설정 저장/복원과 원격 전송 안내를 연결했다. 시작 API가 제공자·모델·설정을 별도로 검증한다. v3가 기본값이다.
- **B8 · 조회와 내보내기:** `/api/runs/{run_id}/tree`와 `/nodes/{node_id}`는 저장된 이벤트를 읽기 전용으로 재생해 노드·점수·확인 상태·시도를 보여준다. 기록 화면의 트리 탭은 노드 상세와 판정 모델/호출/비용을 표시한다. ZIP에는 `session.json`, `events.jsonl`, `tree.json`, `nodes/`, `scorer.json`, `report.json`, 보존 증거를 포함한다. v3에는 트리 API를 제공하지 않는다.
- **실행 코드 오류 방지:** `run_python`의 문법/비동기 호출 및 관찰된 쿼리 인코딩·HTTPConnection 호스트 오류를 실행 전에 검사한다. `run_python`에는 발급된 결과 경로가 있어야 한다. 짧은 `run_command` 탐침은 실행 뒤 같은 시도의 결과 파일을 쓰는 복구 경로를 사용할 수 있다. 하네스가 결과 디렉터리를 준비하고, 잘못된 선택 필드만 있는 JSON은 원본을 보존한 뒤 정규화한다. 증거의 진위는 독립 검증에 맡긴다.

## 확인 결과와 한계

- 관련 단위 테스트에서 로컬 결과 저장·재생, 모델 digest 변경, 입력 상한, 목록 API, v4 시작 API, 트리 조회·ZIP, v3 API를 확인했다. `node --check static/app.js`도 통과했다. 프로젝트 규칙에 따라 화면 검증과 전체 테스트 스위트는 실행하지 않았다.
- 합성 관찰 1건을 설치된 `qwen3.5:9b`로 실제 판정해 `ordinal_label`, 토큰 사용량, 로컬 비용 0을 확인했다. OpenRouter Decisions 카탈로그를 실제 조회했을 때 25개 모델이 필터에 남았고 `typesafe/jev-1.14`가 포함됐다. 이는 품질 성능 검증이 아니다.
- 로컬 실행 모델 실습 run `19f5327eb516473eab18db400456d9f0`, `b26602e1c9b24050b5b0b2d9041d6663`, `7eab1f71aae1420eadcf647c6565e11a`는 모두 후보 0건·oracle 호출 0건이었다. 첫 run에서 모델이 `q=value` 전체를 URL 인코딩해 `q` 매개변수가 사라진 사례, 두 번째 run에서 `HTTPConnection`에 전체 URL을 호스트로 넘긴 사례를 확인해 정적 검사와 프롬프트를 보강했다.
- OpenRouter 실행 모델 `openai/gpt-4.1-mini`와 로컬 판정 모델 `qwen3.5:9b` 조합의 run `a6e24f94f4094de1a0106efd81a1cf6e`는 SQLi 후보 1건을 만들고, 로컬 판정에서 위험도 75점(순서형 등급 3)을 받은 뒤 해당 노드로 하강했다. 후보 검증 시도 1건과 독립 oracle 요청 2건이 기록됐다. oracle의 실습 과제 상태가 전후 모두 미해결이고 canary 조건도 충족하지 않아 결과는 **inconclusive** 1건, confirmed 0건이다. 실행 LLM 비용은 $0.011164, 로컬 판정 API 비용은 0이다. 이는 후보→점수화→하강→독립 검증 경로의 실호출 확인이며, 취약점 발견 정확도나 v3 대비 성능 우위는 입증하지 않는다.
- 중간 실습에서 결과 파일 디렉터리 누락·잘못된 선택 필드·`sql_injection` 별칭·결과 파일 없는 셸 탐침을 확인하고 보정했다. 같은 시도의 결과 파일로만 복구되도록 제한했다. A5의 고정 양성 확인은 모델 발견 성과와 별개의 하네스 검증이다.
- 이 worktree에는 `.env`가 없으므로 OpenRouter를 대시보드에서 실행하려면 이 체크아웃의 `.env` 또는 프로세스의 `OPENROUTER_API_KEY`가 필요하다. 실호출 확인에서는 원본 프로젝트의 기존 키를 메모리에서만 읽었고, 실행 코드의 환경 변수로 내보내지 않았다.
