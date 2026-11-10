# 폴더 구조 정리 실행 계획

> 2026-11-02 작성 · 2026-11-10 수정. Codex가 단독으로 단계별 실행한다.
> 구현 시 `superpowers:executing-plans` 지침을 적용하되, 이 문서의 범위와 `AGENTS.md`의 검증 제한을 우선한다. 체크박스는 구현 진행 기록용이다.

**목표:** 현재 v3·v4 동작을 보존하면서 루트의 Python 모듈을 역할별 패키지로 옮기고, 실행 명령·데이터 경로·기존 기록의 호환성을 유지한다.

**구조:** 실행 기반은 `engine/`, 기존 v4 실행 루프는 `strategies/`, 대시보드는 `web/`, 수동 점검·벤치마크 진입점은 `tools/`에 둔다. 루트 `app.py`는 실행용 진입점으로 남기고 `explorer.py`는 `orchestrator.py`로 이동한다.

**기술:** 기존 Python 3.10+, FastAPI, unittest, Node 내장 테스트 러너를 유지한다. 패키징 도구·의존성·테스트 러너를 추가하거나 교체하지 않는다.

**기준:** 사용자가 확정한 “현재 폴더 구조 정리” 범위, `AGENTS.md`, 구현 시작 시점의 코드와 기존 테스트. 대상 브랜치명이나 working tree 상태는 실행 시 확인한다.

## 0. 범위와 작업 원칙

- 이번 작업은 파일 이동·모듈명·import·리소스 경로·실행 진입점 정리다. v5 스텁·설정 등록·디스패치 추가는 포함하지 않는다.
- `v4_loop.py`는 `V4Runner`, `BudgetGate`와 조사·순회 메서드를 **함께 그대로 이동**한다. 조사부 분해, 클래스명 변경, 전략 등록 테이블 도입, v3 루프 추출은 별도 작업이다.
- 아래 의존성 정리를 위해 `context_char_budget`와 그 상수만 공통 위치로 옮긴다. 계산식·기본 인자·호출 순서는 보존한다.
- 기존 미커밋 변경은 작업 시작 기준의 일부로 기록한다. 다른 작업의 변경을 임의 커밋·stash·reset하거나 덮어쓰지 않는다. 현재 파일 내용을 기준으로 이동한다.
- §2의 이동 대상 파일에 미커밋 변경이 있으면 Phase 1을 시작하지 않고, 해당 파일 목록을 보고해 사용자가 먼저 커밋하도록 요청한다. 같은 파일 안의 사용자 변경과 리팩터 변경을 `git add -p` 등 대화형 명령으로 나누지 않는다. 이동 대상이 아닌 파일의 미커밋 변경은 그대로 두고 커밋에서 제외한다.
- Phase별 변경과 검증 결과를 구분한다. 커밋할 때 해당 단계의 작업만 포함하며, 기존 사용자 변경을 함께 넣지 않는다. 같은 부분을 분리할 수 없는 충돌이 있을 때만 상황을 보고한다.
- 커밋·PR에 Claude/Codex 등 AI 자동 서명을 추가하지 않는다.
- 오류가 나면 해당 단계의 경로·import·호환 처리를 먼저 수정한다. 불확실하다는 이유만으로 전체 Phase를 되돌리거나 flat 구조로 자동 전환하지 않는다.
- 이 문서 수정 자체는 리팩터링 실행이나 커밋을 의미하지 않는다.

## 1. 보존할 동작과 외부 계약

- 허가된 대상 origin 1개, `ControllerLease` 단일 소유, `SessionLog` 단일 writer와 이벤트 순서.
- 기존 로그의 재생·스냅샷·복구, 증거 보존, 런타임 정리 및 실패 시 복구 필요 상태.
- v3·v4의 모델·프롬프트 선택, 조사 순서, scorer 호출·캐시, 예산·중지 조건, 결과와 보고서.
- 후속 관측이 남으면 기존 정책에 따라 조사를 계속하고, 관측 부재·미완료 조사·루트 0표면의 기존 종료 사유를 보존한다.
- 모델 주장·실행 성공과 취약점 확정을 구분하는 현재 계약.
- API URL, 요청·응답 필드, 정적 파일의 공개 URL, 프롬프트 ID·본문·해시 계산.
- 저장 데이터의 `v4_settings`, `v4_usage`, `v4_stage`, 이벤트명, 스키마 버전, 결과·보고서 필드와 파일명.
- `runs/`, `.env`, `.deps/`, `.venv/`는 기존 루트 위치에 둔다. 실사용 로그·키·런타임 데이터를 이동하거나 마이그레이션하지 않는다.

공유 모듈 파일명에서 접두사를 없애는 것은 저장 형식을 개명하는 작업이 아니다. `V4Runner`, `V4BudgetStop`, `finalize_v4`, v4 전용 가드 등 기존 심볼과 동작도 유지한다.

## 2. 목표 구조와 이동표

```text
app.py                      # 기존 python app.py를 유지하는 얇은 진입점
orchestrator.py             # ExplorerV3, 실행·복구·디스패치, 기존 v3 루프
run.cmd
engine/
  __init__.py
  paths.py                  # 프로젝트 루트와 리소스 경로 상수
  session_log.py            command_runtime.py
  harness.py                decision_provider.py
  model_providers.py        model_settings.py
  planning.py               risk_rubric.py
  code_checks.py            scorer_session.py
  scorer_state.py            event_classification.py
  experiment_config.py
  tree.py                   contracts.py
  reporting.py              usage.py
  policy.py                 discovery.py
  lab.py                    scorecard.py
strategies/
  __init__.py
  greedy.py                 # 기존 v4_loop.py 전체, V4Runner 이름 유지
web/
  __init__.py
  app.py                    # 현재 app.py의 구현 전체
  static/                   # 현재 static/ 전체
tools/
  __init__.py
  _legacy_imports.py        # 과거 루프 소스 로딩에만 쓰는 import 호환 처리
  scorecard.py              # engine.scorecard.main으로 위임하는 CLI
  a5_benchmark.py
  check_v4_scorer.py
  check_v4_discovery.py
tests/                      # 현재 배치·파일명·상호 fixture import 유지
docs/  prompts/  benchmarks/  Version/   # 현재 위치 유지
README.md  AGENTS.md  requirements.txt  .env.example  .gitignore
restructure-plan.md  v3plan.md  v4plan.md  v5.md   # 문서 위치 유지
```

| 현재 파일 | 목적지 / 처리 |
| --- | --- |
| `v4_tree.py` | `engine/tree.py` |
| `v4_contracts.py` | `engine/contracts.py` |
| `v4_reporting.py` | `engine/reporting.py` |
| `v4_usage.py` | `engine/usage.py` |
| `v4_policy.py` | `engine/policy.py` |
| `v4_discovery.py` | `engine/discovery.py` |
| `v4_loop.py` | `strategies/greedy.py`, 내부 구현·클래스명 유지 |
| `session_log, command_runtime, harness, decision_provider, model_providers, model_settings, planning, risk_rubric, code_checks, scorer_session, scorer_state, event_classification, experiment_config`의 `.py` 파일 | 같은 이름으로 `engine/` 이동 |
| `lab.py`, `scorecard.py` | 같은 이름으로 `engine/` 이동 |
| `explorer.py` | `orchestrator.py` |
| `app.py`, `static/` | `web/` 이동 후 루트 `app.py`에 실행 진입점 작성 |
| `a5_benchmark.py`, `check_v4_scorer.py`, `check_v4_discovery.py` | 같은 이름으로 `tools/` 이동 |

`lab.py`는 실제 실행·정리·API가 사용하고, `scorecard.py`는 점수 조회 API가 사용하므로 엔진에 둔다. `scorecard.py`의 계산부·CLI 함수는 추가로 분해하지 않고, 도구 패키지에 호출용 진입점만 둔다.

## 3. import와 실행 진입점

### 3.1 의존성 방향

- `web`·`tools`는 `orchestrator`, `strategies`, `engine`을 사용할 수 있다.
- `orchestrator`는 `strategies`와 `engine`을 사용한다.
- `strategies`는 `engine`을 사용한다. `engine`은 `web`, `tools`, `strategies`, `orchestrator`를 import하지 않는다.
- `explorer.context_char_budget(num_ctx: int, system_prompt: str = SYSTEM_PROMPT) -> int`와 `_CHARS_PER_TOKEN`, `_PROMPT_FRACTION`을 `engine/planning.py`로 옮긴다. 기존 프롬프트 상수 정의 뒤에 배치한다. 호출자는 이 위치에서 가져오며, `orchestrator`에는 가져온 심볼이 그대로 노출되어 과거 소스의 호환에도 쓸 수 있다.
- `from engine.tree import TreeStore`처럼 패키지가 명시된 import를 쓴다. 기존 함수 내부 지연 import는 위치를 유지하고 모듈 경로만 바꾼다.
- 각 새 패키지의 `__init__.py`는 비워 둔다. 모든 모듈을 재노출하는 일괄 import를 만들지 않는다.
- 테스트 fixture의 `from test_scorer_session import ...`, `import test_v4_loop`는 유지한다. `tests/__init__.py`를 새로 추가하거나 테스트 구조를 함께 바꾸지 않는다.

### 3.2 실행 명령

루트 `app.py`는 `from web.app import app, main`과 `if __name__ == "__main__": main()`만 가진다. 실제 FastAPI 인스턴스와 `EXPLORER`는 `web.app`에서 한 번만 생성한다.

| 용도 | 프로젝트 루트에서 실행 |
| --- | --- |
| 대시보드 | `python app.py` 또는 기존 `run.cmd` |
| 점수표 | `python -m tools.scorecard [기존 인자]` |
| 벤치마크 | `python -m tools.a5_benchmark [기존 인자]` |
| scorer 점검 | `python -m tools.check_v4_scorer [기존 인자]` |
| discovery 점검 | `python -m tools.check_v4_discovery [기존 인자]` |

`tools/scorecard.py`는 `engine.scorecard.main(sys.argv[1:])`의 반환값을 종료 코드로 사용한다. 도구의 옵션·기본값·실행 조건은 유지한다. 실행 명령의 경로 변경은 README와 현재 사용법 문서에 반영한다.

테스트는 `import app` 대신 `from web import app`으로 실제 구현 모듈을 가져온다. `patch("app...")`도 `patch("web.app...")`로 수정한다. 루트 진입점에서 함수·전역 상태를 복제하거나 `sys.modules` 별칭으로 두 모듈을 같은 이름처럼 보이게 하지 않는다.

## 4. 프로젝트 루트와 리소스 경로

`engine/paths.py`는 아래 `Path` 상수를 제공한다. 표준 라이브러리만 import하며, 디렉터리 생성·파일 읽기·키 로딩을 하지 않는다.

| 상수 | 값 |
| --- | --- |
| `PROJECT_ROOT` | `Path(__file__).resolve().parents[1]` |
| `RUNS_ROOT` | `PROJECT_ROOT / "runs"` |
| `PROMPTS_DIR` | `PROJECT_ROOT / "prompts" / "system"` |
| `ENV_PATH` | `PROJECT_ROOT / ".env"` |
| `DEPS_DIR` | `PROJECT_ROOT / ".deps"` |
| `BENCHMARKS_DIR` | `PROJECT_ROOT / "benchmarks"` |

각 사용처의 기존 변수명·함수 기본 인자는 유지하고, 값의 근거만 이 상수로 바꾼다. 예를 들어 `model_providers.ENV_PATH`는 계속 patch할 수 있어야 한다.

| 사용처 | 처리 |
| --- | --- |
| `planning._PROMPTS_DIR` | `PROMPTS_DIR` 사용. import 시 프롬프트 로딩 성공 확인 |
| `model_providers.ENV_PATH` | 루트 `.env` 유지. 실제 키를 출력하거나 테스트에서 읽지 않음 |
| `explorer/orchestrator.RUNS_ROOT`, `scorecard.RUNS_ROOT` | 같은 `RUNS_ROOT` 사용. 호출자가 주입한 임시 경로는 그대로 존중 |
| `command_runtime._start_browser_worker` | 작업자 인자에 `PROJECT_ROOT` 전달. 작업자가 사용하는 `project / ".deps"`도 기존 위치 유지 |
| `a5_benchmark.CONFIG_PATH` | `BENCHMARKS_DIR / "a5-pair-v2.json"` |
| `check_v4_scorer` 출력 기본 경로 | `RUNS_ROOT / "scorer-contract-probes"` |
| `check_v4_discovery.ROOT` | `PROJECT_ROOT`. Git 명령·diff 범위·미추적 파일 해시가 저장소 전체를 기준으로 동작 |
| `web.app.STATIC_DIR` | 웹 파일을 이동하는 Phase에서 `Path(__file__).resolve().parent / "static"` 유지 |

사용자가 CLI에 입력한 상대 경로의 기존 해석은 바꾸지 않는다. 패키지 내부 데이터만 파일 위치에서 계산한다. `os.chdir()` 또는 전역 `sys.path` 수정을 경로 문제의 우회책으로 사용하지 않는다.

## 5. 과거 소스와 문자열 참조 호환

### 5.1 discovery 비교 도구

`check_v4_discovery`의 `source_info(baseline_ref)`, `runner_type(source, baseline_ref, prompt)` 인터페이스와 반환 형식을 유지한다.

- 현재 코드에서는 `strategies/greedy.py`를 읽고 `V4Runner`를 가져온다.
- 과거 커밋에서는 먼저 `<ref>:v4_loop.py` 존재를 확인하고, 없으면 `<ref>:strategies/greedy.py`를 읽는다. 둘 다 없으면 명확한 로딩 오류를 낸다.
- 프롬프트는 그 커밋의 `prompts/system/v4.txt`를 읽는다. 소스와 프롬프트의 해시는 읽은 원본 bytes로 계산한다.
- 과거 소스 텍스트를 치환하지 않는다. `tools/_legacy_imports.py`의 `baseline_builtins() -> dict`가 반환하는 모듈 전용 builtins 사본을 동적 모듈의 `__builtins__`에 설정한다.
- 그 사본의 `__import__`만 §2의 옛 모듈명 → 새 모듈명으로 해석한다. `explorer`는 `orchestrator`, `v4_loop`는 `strategies.greedy`, 나머지 이동한 런타임 모듈은 해당 `engine.*`로 연결한다. 표에 없는 import와 상대 import는 원래 import 동작에 위임한다.
- 일반 `import harness`, `from v4_tree import TreeStore`, 생성자 내부의 `from explorer import context_char_budget`가 모두 동작해야 한다. 프로세스의 `builtins.__import__`·`sys.path`를 바꾸거나 옛 이름을 `sys.modules`에 등록하지 않는다.
- 주의: 매핑한 이름을 기본 `__import__`에 그대로 넘기면, fromlist가 빈 `import harness`는 `engine.harness`가 아니라 최상위 `engine` 패키지를 반환해 `harness.Harness`가 `AttributeError`가 된다. level 0이고 fromlist가 비어 있는 매핑 대상은 `importlib.import_module(새 이름)`으로 실제 하위 모듈을 반환한다. fromlist가 있는 경우는 새 이름으로 기본 `__import__`에 위임한다.
- 기존 `module.load_prompt` 교체와 프롬프트 해시 처리도 유지한다. 이 호환 기능은 기존처럼 과거 루프·프롬프트를 현재 실행 기반 위에서 비교하는 용도이며, 과거 저장소 전체를 복원하는 기능은 아니다.

이 처리는 엔진 파일을 옮기는 Phase에 함께 적용한다. 비교 도구를 마지막까지 깨진 상태로 두지 않는다.

### 5.2 검색과 수정 범위

`rg`로 일반·지연 import, `mock.patch` 문자열, JS `require`, 직접 파일 읽기, 명령 예제까지 확인한다. 검색 결과를 문맥 없이 일괄 치환하지 않는다.

- `patch("v4_tree.atomic_write_json")` → `patch("engine.tree.atomic_write_json")` 등 **실제 사용 모듈**에 patch한다.
- `tests/model-settings-config.test.cjs`의 `../static/...` → `../web/static/...`.
- 현재 실행·설치 안내는 새 경로로 갱신한다. `docs/a5-benchmark-v1.md`, `docs/v4-scorer-v1.md`의 현재 사용 명령과 모듈 docstring의 CLI 예제도 확인한다.
- 역사적 설계 문서, 과거 Git 경로, 테스트 파일명, 저장 필드·이벤트명은 접두사 제거 대상이 아니다.

완료 기준은 **이동한 모듈·파일을 잘못 가리키는 실행 참조 0건**이다. 호환 매핑과 과거 경로에 남는 옛 이름은 의도된 예외로 기록한다.

## 6. 단계별 실행

모든 Phase는 다음 단계 전에 해당 범위의 검증 결과를 기록한다. 새 파일은 해당 Phase에서만 만들며, 전체 이동표를 첫 Phase에 한꺼번에 수행하지 않는다.

### Phase 0 — 기준 상태와 검증 대상 기록

**대상:** working tree, 기준 커밋, 기존 비화면 테스트. 코드 변경 없음.

- [ ] 현재 브랜치·HEAD·`git status --short`·기존 diff를 확인하고 시작 기준을 기록한다.
- [ ] 미커밋 변경이 있으면 변경 파일과 내용을 보존한다. HEAD만을 작업 전 동작이라고 간주하지 않는다.
- [ ] 미커밋 변경 파일 중 §2 이동 대상이 있으면 §0에 따라 여기서 멈추고 사용자에게 먼저 커밋을 요청한다.
- [ ] 과거 소스 비교에 쓸 이동 전 커밋을 기록하고, 현재 `v4_loop.py`·프롬프트의 원본 해시도 기록한다.
- [ ] §7에서 변경 범위에 해당하는 최소 테스트를 골라 이동 전 결과를 기록한다. 기존 실패는 새 회귀와 구분한다.

**수용 기준:** 시작 상태·관련 테스트 결과·비교할 소스가 식별되고, 이동 대상 파일에 미커밋 변경이 없다. clean 상태를 만들기 위한 Codex의 강제 커밋은 없다.

### Phase 1 — 공통 경로 기준 마련

**생성:** `engine/__init__.py`, `engine/paths.py`, `tests/test_project_paths.py`.
**수정:** §4의 경로 사용처. 파일 이동은 아직 하지 않는다.

- [ ] §4의 경로 상수를 구현하고, 기존 flat 모듈이 이 상수를 사용하도록 바꾼다. `static/`은 Phase 3까지 현재 위치를 유지한다.
- [ ] `test_project_paths.py`에서 작업 디렉터리를 임시 폴더로 바꿔도 프롬프트·설정 기본 경로가 같은 저장소를 가리키는지 확인한다. 테스트는 작업 디렉터리를 복원한다.
- [ ] 브라우저 작업자의 인자 구성은 `Popen`·소켓 등 외부 효과를 모의 처리해 프로젝트 루트가 전달됨을 확인한다. 브라우저 프로세스를 실제로 만들지 않는다.
- [ ] §7의 경로·프롬프트·키 경로 관련 검증을 실행하고 변경 내역을 기록한다.

**제공 인터페이스:** §4의 여섯 `Path` 상수. 이후 Phase는 동일 상수를 재사용한다.
**수용 기준:** 이동 전과 같은 리소스 위치를 사용하고, 호출자 주입 경로와 기존 patch 지점도 유지된다.

### Phase 2 — 엔진·기존 전략·오케스트레이터 이동

**이동:** §2의 `engine/` 대상 전체, `v4_loop.py` → `strategies/greedy.py`, `explorer.py` → `orchestrator.py`.
**생성:** 비어 있는 `strategies/__init__.py`, `tools/__init__.py`, `tools/_legacy_imports.py`, `tools/scorecard.py`, `tests/test_baseline_imports.py`, `tests/test_entrypoints.py`.
**수정:** 모든 호출자의 해당 import·patch 경로, 아직 루트에 있는 점검 도구·앱, README의 관련 사용법.

- [ ] 추적 파일을 `git mv`로 이동하고, 각 파일의 내부 로직과 기존 심볼 이름을 유지한다.
- [ ] §3.1대로 `context_char_budget`와 상수를 `engine/planning.py`로 옮기고 호출자를 갱신한다.
- [ ] 일반·지연 import와 patch 문자열을 갱신한다. 테스트 파일명·fixture 모듈명은 유지한다.
- [ ] §5.1의 과거 소스 import 호환과 현재 소스 경로 변경을 루트 `check_v4_discovery.py`에 적용한다.
- [ ] `test_baseline_imports.py`에서 일반/from/함수 내부 import(일반 `import harness`가 `engine` 패키지가 아니라 `engine.harness` 모듈에 묶이는지 포함), 원본 소스 해시 보존, 전역 import 상태 비변경, 과거·현재 Git 경로 선택과 파일 부재 오류를 검증한다. Git·모델·런타임은 필요한 곳에서 모의 처리한다.
- [ ] Phase 0에서 기록한 실제 과거 루프도 임시 로그·가짜 runtime·scorer로 로딩·생성할 수 있는지 확인한다. 실험 실행이나 모델 호출 없이 생성자 내부 import까지 확인한다.
- [ ] `test_entrypoints.py`에 scorecard 진입점의 인자·종료 코드 위임을 확인하는 테스트를 추가한다. `engine.scorecard.main`을 mock하여 실제 run을 읽지 않는다.
- [ ] §7의 엔진·전략·복구 관련 검증을 실행한다. 진행 중 파일 이동에 따라 `test_project_paths.py`의 import만 새 경로로 맞춘다.

**제공 인터페이스:** 기존 `ExplorerV3`, `V4Runner`, `BudgetGate` 및 엔진 심볼의 새 import 경로, `baseline_builtins() -> dict`.
**수용 기준:** 루트 앱·점검 도구가 계속 로딩되고, v3·v4 동작과 과거 소스 비교 기능이 보존된다. 엔진에서 상위 계층으로 향하는 import가 없다.

### Phase 3 — 웹 구현과 정적 파일 이동

**이동:** 현재 `app.py` → `web/app.py`, `static/` → `web/static/`.
**생성:** 비어 있는 `web/__init__.py`, 루트 실행용 `app.py`.
**수정:** `tests/test_entrypoints.py`, 웹 모듈을 사용하는 테스트 전체의 import·patch 경로, JS 단위 테스트 경로, README 구성 안내. `run.cmd`의 기존 명령은 유지한다.

- [ ] 웹 구현·기존 보조 클래스·함수를 함께 이동하고, 공개 라우트와 정적 URL을 보존한다.
- [ ] §3.2의 루트 진입점을 작성한다. 모든 웹 테스트가 실제 `web.app`의 전역 상태를 교체하도록 수정한다.
- [ ] `test_entrypoints.py`에서 루트가 동일 FastAPI 객체를 노출하는지, 실행 시 `web.app.main`에 한 번 위임하는지 모의 처리로 확인한다. 실제 서버는 띄우지 않는다.
- [ ] `test_entrypoints.py`에서 FastAPI TestClient로 `/`, `/app.js`, `/style.css`, `/model-settings-config.js`, `/runs/{run_id}`의 응답 내용·타입을 임시 저장소와 함께 확인한다. HTML의 기존 쿼리 문자열을 붙인 요청도 성공해야 한다.
- [ ] §7의 API·정적 파일·JS 로직 검증을 실행한다.

**제공 인터페이스:** `web.app.app`, `web.app.main()`, 루트 `app.app` 및 기존 실행 명령.
**수용 기준:** 브라우저 없이 진입점 위임과 HTTP 응답을 확인하고, 기존 URL·실행 명령이 유지된다.

### Phase 4 — 점검 도구 이동과 참조 마무리

**이동:** `a5_benchmark.py`, `check_v4_scorer.py`, `check_v4_discovery.py` → `tools/`.
**수정:** 관련 테스트 import·patch, README·현재 사용 문서·CLI docstring, `tests/test_entrypoints.py`.

- [ ] 세 도구를 옮기고 모듈 실행 방식으로 문서를 갱신한다. Phase 2에서 추가한 호환 로딩과 루트 경로 계산을 유지한다.
- [ ] argparse를 쓰는 세 도구의 `--help`가 종료 코드 0으로 끝나는지 확인한다. discovery의 기본 dry-run도 실제 run을 읽으므로 도움말을 넘어서 자동 실행하지 않는다.
- [ ] scorecard 진입점은 계산 함수·입력 run 목록을 모의 처리해 인자와 종료 코드 위임을 확인한다. 사용자 실제 run을 검증 데이터로 사용하지 않는다.
- [ ] §7의 도구 관련 검증을 실행하고, §5.2의 검색으로 잘못된 모듈·파일 참조를 정리한다.
- [ ] §8 완료 기준과 Phase별 결과를 대조해 완료·미검증 범위를 기록한다.

**수용 기준:** 모든 파일이 목표 위치에 있고, 앱·도구 실행 경로와 안내가 일치한다. 과거 Git 경로와 저장 데이터의 이름은 보존된다.

## 7. 검증 범위와 실행 방법

이 절은 **향후 구현 검증 계획**이다. 계획 문서 편집만으로 테스트 실행이나 통과를 주장하지 않는다.

- 기존 `unittest` 러너를 사용한다. Phase마다 아래 관련 항목만 실행하며, 프로젝트 전체 discover나 전체 회귀 테스트는 사용자가 명시 요청했을 때만 실행한다.
- 브라우저 실행·Playwright UI 테스트·스크린샷·렌더링 검증은 명시 요청이 있어야 한다. 도구 smoke check에서도 실험을 시작하지 않는다.
- 모델·Docker·대상 HTTP 호출은 fake/mock으로 대체하고 임시 run 디렉터리를 사용한다. `--execute`, `--live`, 실제 benchmark 실행은 이 계획의 기본 검증에 포함하지 않는다.
- `test_command_runtime.py`는 `RuntimeTests`만 선택한다. 같은 파일의 `BrowserWorkerTests`를 실행하지 않는다.
- `test_v3_ui_integration.py`, `test_v3_real_ollama.py`, `test_browser_planner.py` 등의 브라우저·실모델 검증은 자동 실행 대상에서 제외한다. 이름에 browser가 있어도 fake runtime만 쓰는 `test_v4_loop.py` 케이스는 코드로 확인한 뒤 실행할 수 있다.
- 관련 테스트는 기존의 실제 동작 검증을 재사용한다. 추가 테스트는 경로·진입점·동적 소스 로딩처럼 이동으로 새로 생기는 경계에 한정한다.

| Phase | 최소 검증 대상 |
| --- | --- |
| 1 | `test_project_paths.py`, `test_planner.py`; `test_openrouter.OpenRouterTests.test_dotenv_key_is_not_added_to_process_environment_and_env_takes_precedence`; `test_a5_benchmark.A5BenchmarkTests.test_protocol_is_explicit_and_local_model_is_required` |
| 2 | `test_v4_loop.py`, `test_v4_discovery_context.py`, `test_v4_tree.py`, `test_v4_reporting.py`, `test_baseline_imports.py`, `test_entrypoints.py`, `test_v3_explorer.py`; `test_command_runtime.RuntimeTests`; `test_scorer_session.ScorerSessionTests.test_all_siblings_batched_before_ordering_and_cache_survives_replay` |
| 3 | `test_entrypoints.py`, `test_v3_api.py`, `test_v4_execution_api.py`, `test_v4_tree_api.py`, `tests/model-settings-config.test.cjs` |
| 4 | `test_a5_benchmark.py`, `test_baseline_imports.py`, `test_entrypoints.py`; 세 점검·벤치마크 도구의 `--help` |

Phase 0에서는 위의 **기존** 테스트 중 구현 단계에서 사용할 대상을 미리 기록한다. 새 테스트는 해당 Phase에서 추가한다. 추가 변경이나 실패로 확인이 필요한 경우에만 관련 검증을 넓힌다.

프로젝트 루트에서 파일 하나를 선택하는 예:

```powershell
python -m unittest discover -s tests -p test_v4_loop.py -v
```

상호 fixture import를 유지하면서 클래스·메서드를 선택하는 예:

```powershell
python -c "import sys, unittest; sys.path.insert(0, 'tests'); unittest.main(module=None, argv=['unittest', 'test_command_runtime.RuntimeTests', '-v'])"
node --test tests/model-settings-config.test.cjs
```

다른 선택 대상은 첫 명령의 패턴 또는 둘째 명령의 테스트 식별자만 바꾼다. 이 프로젝트에는 `.venv`가 없으며, 시스템 `python`과 루트 `.deps`(브라우저 작업자가 사용)를 쓴다. `python`이 PATH에 없으면 대시보드를 실행하는 것과 같은 인터프리터를 사용한다. 기대 결과는 종료 코드 0과 테스트의 `OK`/pass이며, 실패·skip은 그대로 기록한다.

모의 입력 비교에서 보존할 항목은 노드·후보 발견 및 선택 순서, attempt 연결, 이벤트 종류·순서, 결과·종료 사유, 보고서 의미와 예산 집계다. UUID·시각·임시 절대 경로 등 비결정적 값만 정규화한다. 실제 LLM 실행 1건을 리팩터링의 동등성 기준으로 삼지 않는다.

추가 경계 테스트의 점검 초점은 다음 다섯 가지다.

1. 다른 작업 디렉터리에서도 프롬프트·리소스 기본 경로가 유지됨 — Phase 1.
2. 브라우저를 띄우지 않고도 작업자에게 전달할 루트 경로가 올바름 — Phase 1.
3. 과거 루프의 함수 내부 import까지 로딩되며 전역 import 상태를 오염시키지 않음 — Phase 2.
4. 앱 인스턴스·전역 상태가 중복 생성되지 않고 기존 정적 URL이 유지됨 — Phase 3.
5. 도구의 도움말·CLI 위임이 실험·실모델 호출 없이 작동함 — Phase 4.

## 8. 완료 기준

- [ ] §2의 이동 목록에 누락이 없고, 새 패키지와 실행 진입점이 최종 구조와 일치한다.
- [ ] 조사·순회 알고리즘, 클래스명, 지원 모드, API·로그·보고서 계약이 §1대로 보존된다.
- [ ] 실제 실행 코드·테스트의 잘못된 import·patch·파일 경로 참조가 없다. 과거 Git 경로·호환 매핑·문서상 옛 이름은 예외로 식별된다.
- [ ] `engine`이 상위 계층을 import하지 않고, 패키지 초기화가 새 순환 import를 만들지 않는다.
- [ ] 루트 `python app.py`·`run.cmd`와 문서화한 도구 명령이 지원된다. 정적 리소스의 공개 URL과 루트 데이터 경로가 유지된다.
- [ ] 과거 소스 비교 도구가 현재·과거 루프 경로와 옛 모듈 import를 처리한다.
- [ ] Phase별 관련 비화면 검증 결과가 기록되고, 기존 실패·새 실패·미실행 항목이 구분된다.
- [ ] 기존 사용자 변경과 실사용 데이터를 보존하고, 변경 내역을 Phase별로 검토·되돌릴 수 있다.
