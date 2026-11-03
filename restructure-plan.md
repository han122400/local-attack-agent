# 리팩터링 플랜 — 공유 엔진 / 전략(플래너) 분리 및 구조 정리 (v5 준비)

> 2026-11-02 작성. 대상 브랜치: `v5`. 이 문서는 **Codex가 단독으로 실행**하도록 쓰였다.
> 목표는 "탐색 전략(loop/플래너)만 교체하면 알고리즘이 바뀌는" 구조를 만드는 것이며,
> 이는 아직 미구현인 v5(`v5.md`의 전역 frontier 스케줄러)를 깨끗하게 올리기 위한 **선행 리팩터링**이다.
> **이 플랜 자체는 v5 알고리즘을 구현하지 않는다.** v5 신규 작성은 마지막 Phase 4에서 자리만 잡고, 실제 알고리즘은 별도 작업으로 남긴다.

## 0. Codex 작업 지침 (먼저 읽을 것)

- **동작 보존이 최우선.** Phase 1~3은 리팩터링이다. 로직/행동을 바꾸지 않는다. 이름·위치·import만 바꾼다.
- **Phase 단위로 커밋한다.** 각 Phase는 독립적으로 테스트 통과 상태에서 끝나야 한다. 섞지 말 것.
- **리팩터링과 신기능을 섞지 말 것.** v5 알고리즘 작성은 Phase 4 범위 밖이다.
- **각 Phase 후 영향받는 테스트를 돌려 동작 보존을 확인한다.** 전체 스위트 실행은 사용자 승인이 필요하다(아래 §8 프로젝트 규칙). 리팩터링 검증 목적의 테스트 실행은 필요하며, 프로젝트가 쓰는 기존 러너를 그대로 사용한다.
- **불확실하면 멈추고 보고한다.** 특히 import 메커니즘(§7)에서 `python app.py`와 테스트 러너가 깨지면 그 Phase를 되돌리고 보고.

## 1. 배경과 목표

현재 `experiment_mode`는 `"v3"`, `"v4"` 두 전략을 지원한다. 디스패치는 `explorer.py`:

```python
if log.metadata.get("experiment_mode") == "v4":
    from v4_loop import V4Runner
    result, reason = V4Runner(log, runtime, ...).run()
else:
    result, reason = self._loop(run_id, log, runtime)   # v3
```

v5는 v4의 하네스·scorer·저장·리포팅·조사 계층을 **그대로 재사용**하고 "다음에 어느 노드를 보고 언제 후보를 테스트할지"(순회/선택 = 플래너)만 교체한다(`v5.md` §1, §4, §4.6). 그런데 지금은:

- 그 "플래너"가 별도 모듈이 아니라 `v4_loop.py`의 `V4Runner.run()` 안에 박혀 있고, **공유 조사 로직**(`_investigate`/`_context`/`_made_progress`/`BudgetGate`)과 **한 파일에 섞여** 있다.
- `v4_*` 접두사가 붙은 모듈 상당수가 **사실은 버전 무관 공유 엔진**이라, 네이밍이 소유관계를 오도한다.

목표: 공유 엔진과 전략(플래너)을 물리적으로 분리해 **`strategies/`의 loop 파일 선택 = 알고리즘 선택**이 성립하게 한다.

## 2. 핵심 원칙 (불변 — 리팩터링이 깨면 안 되는 것)

`v5.md` §4.6과 동일하다. 리팩터링 후에도 유지:

- 허가된 대상 origin 1개, `ControllerLease` 단일 소유.
- `SessionLog` 단일 writer·이벤트 전순서·결정적 재생(`latest_event_seq` 기반 리플레이·스냅샷).
- 모델 주장·위험도 점수·실행 성공과 취약점 확정의 구분. 자동 취약점 판정 없음.
- 완료 조건(2026-11-02 반영): 후속 관측이 남으면 노드 한도까지 이어 조사, 루트 0표면·후속 잔존은 `incomplete` 보고.

## 3. 현재 구조의 문제 (핵심 통찰)

`v4_` 접두사 6개 중 **진짜 v4 전용은 1개뿐**이다:

| 파일 | 실제 성격 | v5가 재사용? |
| --- | --- | --- |
| `v4_tree.py` | 공유 엔진 (트리/이벤트 재생, `TreeStore`, `replay_tree`, `latest_observation`, `investigation_gaps`) | ✅ |
| `v4_contracts.py` | 공유 (결과·발견 스키마, `validate_result`, `dedup_key`) | ✅ |
| `v4_reporting.py` | 공유 (리포팅) | ✅ |
| `v4_usage.py` | 공유 (사용량 집계) | ✅ |
| `v4_policy.py` | 공유 (실행 한도 `execution_settings`, `V4BudgetStop`) | ✅ |
| `v4_loop.py` | **혼합**: `run()`=v4 전용 알고리즘 + `_investigate` 등=공유 조사 | 부분 |

진짜 v4 전용 알고리즘은 `v4_loop.py`의 `run()` 하나다.

## 4. 목표 구조

```
engine/              # 버전 무관 공유 (전부 v5가 재사용)
  __init__.py
  session_log.py        command_runtime.py   harness.py
  decision_provider.py  model_providers.py   model_settings.py
  planning.py           risk_rubric.py       code_checks.py
  scorer_session.py     scorer_state.py      event_classification.py
  experiment_config.py
  tree.py        ← v4_tree.py
  contracts.py   ← v4_contracts.py
  reporting.py   ← v4_reporting.py
  usage.py       ← v4_usage.py
  policy.py      ← v4_policy.py
  investigation.py  ← (신규) v4_loop.py에서 추출: _investigate/_context/_made_progress/
                       _validate_command/_import_result/BudgetGate 등 '한 노드를 파는' 공유 로직

strategies/          # 유일하게 버전마다 다른 '플래너' = 순회/선택 loop
  __init__.py
  greedy.py      ← v4_loop.py의 run()/V4Runner (→ GreedyRunner). engine.investigation 사용
  v3.py          ← explorer.py의 self._loop 추출(선택; 위험하면 Phase 3에서 보류 가능)
  frontier.py    ← (신규, Phase 4) v5: 전역 큐 + value backup + 선점. 자리만 잡고 NotImplemented

orchestrator.py  ← explorer.py (ExplorerV3: 디스패치/lease/store). mode→strategy 매핑
web/
  app.py  +  static/
tools/           # 런타임 아님 (벤치마크·점검·랩)
  a5_benchmark.py  scorecard.py  check_v4_scorer.py  lab.py
docs/  prompts/  tests/   (유지)
```

> 폴더 분리가 §7 import 메커니즘 때문에 과도하게 위험하면, **flat 유지 + 이름만 변경**(예: `v4_tree.py`→`tree.py`)으로 대체해도 목표(접두사 제거·전략 분리)는 달성된다. 폴더화는 선택적 상위 목표다. 어느 쪽을 택했는지 커밋 메시지에 남길 것.

## 5. 이동/이름 매핑표 (정본)

접두사 제거(필수):

| 현재 | 목표 |
| --- | --- |
| `v4_tree.py` | `engine/tree.py` (flat 시 `tree.py`) |
| `v4_contracts.py` | `engine/contracts.py` |
| `v4_reporting.py` | `engine/reporting.py` |
| `v4_usage.py` | `engine/usage.py` |
| `v4_policy.py` | `engine/policy.py` |
| `v4_loop.py` | 분해 → `engine/investigation.py` + `strategies/greedy.py` |

공유 엔진 이동(이름 유지, 폴더만):
`session_log, command_runtime, harness, decision_provider, model_providers, model_settings, planning, risk_rubric, code_checks, scorer_session, scorer_state, event_classification, experiment_config` → `engine/`

진입/오케스트레이션: `explorer.py`→`orchestrator.py`, `app.py`→`web/app.py`(+`static/`), 툴 4종→`tools/`.

> `experiment_config.py`에 experiment_mode 허용값(`v3`,`v4`)이 있다. Phase 4에서 `v5` 추가 지점이다.

## 6. 단계별 작업

### Phase 0 — 베이스 정리 (선행 필수)
- 현재 working tree에 커밋 안 된 `v4_*` 변경이 있다. **먼저 커밋**해 깨끗한 베이스를 만든다(안 그러면 rename과 수정이 엉켜 git rename 감지·리뷰가 망가진다).
- 기준 테스트를 돌려 **현재 통과 상태를 기록**한다(리팩터링 전후 비교 기준).
- 수용 기준: 작업 트리 clean, 기준 테스트 결과 기록됨.

### Phase 1 — 접두사 제거 + (선택)폴더화 [동작 보존]
- §5 매핑대로 파일을 `git mv`로 이동/개명. **파일 내용(로직) 변경 금지.**
- import 갱신: 전 코드·tests에서 옛 모듈명을 새 이름으로. 열거하지 말고 `grep`으로 찾아 일괄 치환 후 잔존 확인:
  - 예: `git grep -nE "v4_tree|v4_contracts|v4_reporting|v4_usage|v4_policy"` → 0건 될 때까지.
- §7 import 메커니즘 처리.
- 검증: `python app.py`가 기동되고(즉시 종료해도 import 에러 없을 것), 영향 테스트 통과.
- 수용 기준: 옛 모듈명 참조 0건, 기동/테스트 Phase 0과 동일 결과.

### Phase 2 — 공유 조사 로직 추출 [동작 보존]
- `v4_loop.py`에서 **한 노드를 파는 공유 로직**을 `engine/investigation.py`로 추출:
  `_investigate, _context, _made_progress, _validate_command, _import_result, BudgetGate`, 그리고 이들이 쓰는 헬퍼.
- `run()`(순회/선택)은 남겨 둔다 → 이게 Phase 3에서 `strategies/greedy.py`가 된다.
- 추출 방식: `V4Runner`를 (a) 공유 조사부를 가진 베이스(또는 `NodeInvestigator` 협력 객체)와 (b) 전략부로 나눈다. 전략은 조사부를 **호출**만 하도록, DFS 가정(주석의 "descendants are the harness's next work" 등)을 전략 쪽으로 몰아낸다.
- 검증: 동일. 특히 `test_v4_loop.py`·`test_v4_attempts.py`·`test_v4_execution_api.py`가 그대로 통과해야 한다(필요 시 import 경로만 갱신).
- 수용 기준: v4 런 1건의 이벤트 로그/리포트가 Phase 1과 바이트 수준까진 아니어도 **의미상 동일**(표면/후보/incomplete 사유 동일).

### Phase 3 — 전략 패키지화 [동작 보존]
- `run()`+`V4Runner` 잔여부 → `strategies/greedy.py`(`GreedyRunner`). `engine.investigation`을 사용.
- `orchestrator.py`(옛 `explorer.py`)의 디스패치를 **mode→strategy 매핑 테이블**로 정리:
  ```python
  STRATEGIES = {"v4": "strategies.greedy:GreedyRunner"}  # 예시
  ```
- (선택) v3 loop(`self._loop`)도 `strategies/v3.py`로 추출. **위험하면 보류**하고 기존 위치 유지 — v3는 결과 계약이 달라 섣불리 건드리지 말 것.
- 검증: v3·v4 두 모드 모두 기동/테스트 통과.
- 수용 기준: `experiment_mode` v3·v4가 리팩터링 전과 동일 동작.

### Phase 4 — v5 자리 잡기 (알고리즘 미구현)
- `strategies/frontier.py` 생성: `FrontierRunner`가 `engine.investigation`을 재사용하고, `run()`만 **전역 frontier 큐 + value backup + 선점**으로 구현될 자리. **본체는 `NotImplementedError`로 두고** 실제 알고리즘은 별도 작업(`v5.md` §4·§8 값 확정 필요).
- `experiment_config.py` 허용값에 `"v5"` 추가, 디스패치 매핑에 `"v5": FrontierRunner` 등록.
- scorer 호출 맥락 전환(형제 집합→frontier) 지점을 주석 `# TODO(v5 §8)`로 표시만.
- 수용 기준: `experiment_mode="v5"`가 설정 검증을 통과하고 디스패치되나, 실행 시 명확한 "미구현" 에러를 낸다. v3·v4는 영향 없음.

## 7. import 메커니즘 주의 (현재 flat → 폴더화 시)

현 상태: 진입점 `run.cmd → python app.py`(repo 루트), 모든 import가 flat(`from v4_tree import ...`), `__init__.py`/`conftest`/`pyproject` 없음. 테스트도 flat이며 서로 import(`from test_scorer_session import ...`).

- **flat 유지(이름만 변경) 방식**이면 import 메커니즘은 안 바뀐다 — 모듈명 치환만. 가장 안전.
- **폴더화** 방식이면:
  - 각 폴더에 `__init__.py` 추가, import를 패키지 한정형으로(`from engine.tree import ...`).
  - `python app.py`를 루트에서 실행하므로 루트가 `sys.path`에 있어 `import engine.*`는 동작한다. `web/app.py`로 옮기면 진입 방식(`run.cmd`)과 app.py 내부의 상대 경로(`static/`, `runs/`, `prompts/`) 기준이 바뀌므로 **경로 기준을 깨지 않게** 처리(예: 루트 기준 `Path(__file__)` 재계산). 애매하면 `app.py`는 루트에 두고 `web/`에는 `static/`만 옮기거나, 이동을 보류.
  - 테스트 러너의 rootdir/`sys.path`를 확인하고, 테스트가 서로를 import하는 부분이 깨지지 않게 한다. 깨지면 그 이동을 되돌리고 flat 대안으로.
- **원칙**: 폴더화가 `python app.py` 또는 테스트 러너를 깨면, 그 부분은 flat 대안으로 내려간다. 구조 미관보다 동작 보존이 우선.

## 8. 하지 말 것 / 프로젝트 규칙

- Phase 1~3에서 **로직 변경 금지**(버그로 보여도 고치지 말고 별도 보고). 이름/위치/import만.
- v5 알고리즘을 Phase 4에서 **구현하지 말 것**(자리만).
- `AGENTS.md`(프로젝트 규칙) 준수:
  - 화면 검증(브라우저/Playwright/스크린샷/렌더링)은 사용자가 명시 요청할 때만.
  - **전체 테스트 스위트**는 사용자가 명시 요청할 때만. 리팩터링 검증은 **영향받는 테스트** 중심으로 하고, 전체 실행이 필요하면 사용자 승인을 받는다.
  - 검증 안 했으면 했다고 하지 말 것.
- GitHub 커밋/PR에 AI 자동 서명(Claude/Codex 등) 추가 금지(사용자 전역 규칙).

## 9. 완료 정의 (전체)

1. `v4_` 접두사 공유 모듈 참조 0건.
2. 공유 조사 로직이 전략과 분리됨(`engine/investigation.py`).
3. `strategies/`에서 loop 파일 교체만으로 전략이 바뀌는 구조.
4. v3·v4 모드가 리팩터링 전과 동일 동작(영향 테스트 통과).
5. `"v5"`가 설정·디스패치에 등록되고 미구현임이 명확히 드러남.
6. 각 Phase가 개별 커밋으로 남아 되돌리기 가능.
