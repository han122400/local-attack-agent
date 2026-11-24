# v4.6 HTTP 관측 수집

## 기본 동작

| 경로 | 수집 | 기존 검증기 입력 |
| --- | --- | --- |
| HTTP 대상, 환경변수 프록시를 사용하는 일반 클라이언트 | 대상 origin의 실제 upstream HTTP/1 바이트 | `http_exchange_recorded` |
| 기존 `audit_http` / `http_capture_client.request` | 기존 CaptureSession 직접 전송 유지 | 기존과 동일 |
| HTTPS 직접 호출 / CONNECT | MITM 없이 전달, 평문 미수집 | 없음 |
| HTTP/2 등 현재 증거 파서가 읽지 못하는 전송 | 가능한 불투명 전달, 검증용 수집 제외 | 없음 |
| HTTPS 브라우저, 기본 모드 | 실제 응답 이벤트의 파싱 자료를 별도 미검증 감사 파일로 저장 | 없음 |
| 명시적 MITM 옵트인 | 대상 origin의 파싱·재구성 스냅샷 | 없음: 별도 감사 파일만 저장 |

기본 프록시는 Python 표준 라이브러리로 구현한 HTTP 전달기다. TLS를 해석하지 않으며, CONNECT는 불투명 스트림으로 전달한다. HTTP/2를 HTTP/1 증거로 재구성하지 않는다. HTTP/1 요청은 explicit proxy의 absolute-form 요청 줄을 origin-form으로 바꿔 전송하고, **실제로 origin에 보낸 바이트**를 보존한다. 응답은 최종 HTTP/1 메시지의 청크 경계·확장·트레일러를 보존한다.

프록시는 요청 시작 때 exec에 결합한다. 활성 exec가 없거나 종료 후 완료한 교환은 기록하지 않는다. 기록 큐는 전송 경로와 분리되고, exec 종료 이벤트 전에 이미 완료한 기록을 배출한다. 수집 한도 초과, 파서가 지원하지 못하는 교환, 관측 콜백 오류는 수집을 포기하는 조건이다. 네트워크 오류가 있는 실제 대상의 성공까지 보장하는 것은 아니다.

기존 헬퍼는 프록시 환경변수를 읽지 않는 직접 소켓 전송을 사용한다. 따라서 헬퍼와 프록시는 요청별로 서로 다른 경로를 담당하며 중복 이벤트를 만들지 않는다. 원래 헬퍼의 범위·예산 규칙은 그대로다. 일반 코드·도구에 헬퍼 사용을 강제하지 않는다.

## 실행 환경과 관측 ID

HTTP 대상의 신규 지원 정책 실행에서 프록시는 브라우저 시작 전에 열리고 runtime 종료 때 닫힌다. `HTTP_PROXY`, `HTTPS_PROXY`와 소문자 변형을 자식 프로세스에만 설정한다. 로컬 대상 수집을 위해 자식의 `NO_PROXY`는 비운다. **ALL_PROXY는 덮어쓰지 않는다.** 다른 프로토콜을 HTTP 관찰자로 강제 전달하지 않기 위한 선택이다. 직접 연결이나 클라이언트 자체 우회 설정을 금지하지 않는다.

`HARNESS_HTTP_CAPTURE_INDEX`는 현재 exec의 비동기 조회용 JSON 파일이다. `requests` 항목에 `request_id`와 `facts`가 들어간다. 응답 전달과 기록은 비동기이므로 직후 조회가 비어 있을 수 있다. 실행 결과 및 v4 도구 이력에도 최근 관측 ID를 제공한다. 이 파일은 에이전트가 읽기 위한 복사본이며, 검증기는 이 파일을 신뢰하거나 import하지 않는다. 검증 기준은 부모 프로세스가 기록한 저널과 보존 바이트다.

## 선택적 MITM

기본값은 OFF다. 운영자가 Python runtime API를 명시적으로 호출할 때만 켠다. UI/CLI 토글은 추가하지 않았다.

```python
runtime = CommandRuntime(runs_root, log, mitm_capture=True)
```

이 플래그는 첫 TLS 피닝/인증서 거부 연결의 실패 가능성과 재구성 자료 수집에 대한 명시적 옵트인이다. 실패한 연결을 재실행하지 않으며, 해당 호스트의 다음 연결은 MITM을 우회한다.

메인 프로그램의 Python 3.10 요구사항은 유지한다. 선택적 mitmproxy 12.3.1은 별도 Python 3.12 이상 환경을 사용한다. Windows 설치 예:

```powershell
py -3.13 -m venv .deps/mitm-env
.deps/mitm-env/Scripts/python.exe -m pip install -r requirements-mitm.txt
```

별도 자식 프로세스가 DumpMaster를 임베드한다. 준비 실패 시 실행을 막지 않고 기본 HTTP 프록시 또는 직접 연결로 돌아간다. 실행 시작 전 MITM 결합 실패도 죽은 프록시 환경을 제거한다. 이미 시작한 요청은 재시도하지 않는다. 브라우저가 죽은 프록시를 사용 중이었다면 다음 코드 실행 전에 worker를 재시작하므로 브라우저 상태가 초기화될 수 있다. 이 브라우저 복구 경로는 실제 브라우저로 검증하지 않았다.

CA는 해당 run의 `_ca`에만 만든다. OS 신뢰 저장소는 수정하지 않는다. 기존 시스템·명시적 인증서 번들에 run CA를 더한 번들을 자식 프로세스 환경변수로 전달한다. 브라우저는 옵트인 때만 해당 context의 `ignore_https_errors=True`를 사용한다. 이는 CA 한 개만 신뢰하는 것보다 넓은 허용이며, 기본 모드에서는 사용하지 않는다. 브라우저 실행 검증은 하지 않았다.

재구성 자료는 `artifacts/http-observed`의 JSON과 `http_exchange_observed` 감사 이벤트로만 저장한다. `representation=mitmproxy_reconstructed`, `literal=false`, `verification_eligible=false`를 명시한다. **harness_http 증거 ref 또는 http_exchange_recorded 이벤트를 만들지 않는다.** 기존 검증·점수·보고 계층이 재구성 자료를 원본처럼 받아들이는 경로는 없다.

## HTTPS 브라우저 감사

사용자가 승인한 수동 이벤트 방식으로 구현했다. `request` 이벤트에서 현재 exec에 결합하고, 실제 `response` 이벤트 객체의 status/headers_array()/body()에서만 응답 자료를 얻는다. `route.fetch()`나 별도 HTTP 요청을 사용하지 않는다. 기존 route 가드는 유지하며 감사 수집기가 요청을 재수행·중단·변경하지 않는다.

부모 런타임은 worker가 돌려준 자료를 별도 감사 저장기로만 전달한다. 파일·이벤트·artifact ref 모두 `source=browser_response_audit`이며, `representation=playwright_decoded`, `literal=false`, `verification_eligible=false`와 **재구성·미검증: 파싱본이며 literal 와이어 바이트가 아님** 라벨을 붙인다. `http_exchange_recorded`, `harness_http` ref, 검증용 request ID를 만들지 않는다. 기존 트리 리듀서가 소비하는 증거·관측 입력과 연결되지 않으므로 점수·보고의 verified 통계에 들어가지 않는다.

수집은 exec당 최대 32개, body 64 KiB, 총 512 KiB, header 16 KiB, 동시 수집 task 4개로 제한한다. 압축·길이 불명·큰 응답은 body를 읽지 않고 생략 사유를 기록한다. exec 종료 때 이미 시작한 수집만 최대 200ms 기다리고, 늦은 응답을 다음 exec에 옮겨 붙이지 않는다. 취소 대상은 감사 수집 task이며 원래 브라우저 요청이 아니다. 수집·선택적 파일 저장 실패는 실행 실패로 바꾸지 않는다.

점수에 반영할 HTTPS 데이터 관측이 필요하면 처음 요청을 계획할 때 `audit_http`/`http_capture_client.request`를 선택하고 반환된 request_id를 인용하도록 시스템 프롬프트와 실행 컨텍스트를 보완했다. 브라우저 감사 파일은 가시성 전용이며, 증거를 얻으려고 상태 변경 요청을 자동 재실행하지 않도록 명시했다.

실제 브라우저 실행은 수행하지 않았다. 이벤트 API 대역을 사용하는 수집 단위 테스트와 worker 제어 메시지 경계의 런타임 테스트로 수집·저장·검증 격리를 확인했다.

참고: https://playwright.dev/python/docs/api/class-route#route-fetch

## report.json의 미검증 관측 목록

`observed_http`는 브라우저와 MITM 감사 이벤트를 함께 보여 주는 별도 목록이다. 각 항목의 source와 representation으로 출처를 구분하며, literal=false, verification_eligible=false와 재구성·미검증 라벨을 유지한다. 기존 `http_audit`와도 다른 키이고, stats·run_result·candidate_leads·evidence_refs·unexplored_high_risk·seed_metrics에는 합산하거나 참조하지 않는다.

새 `http_exchange_observed` 이벤트에는 URL·method·status만 추가한다. 본문·헤더는 감사 artifact에 남긴다. 이벤트의 URL은 표시용으로 사용자 정보와 fragment를 제거하고 모든 쿼리 값을 `[redacted]`로 가린다. 전체 URL은 artifact에만 있다. 이전 이벤트에 메타데이터가 없으면 보고서에는 null을 넣고 artifact를 열어 보충하지 않는다.

보고서는 readable_prefix의 이벤트만으로 목록을 조립한다. 관측 이벤트가 있으면 현재 scoring policy와 무관하게 노출한다. 이 보고 변경은 수집기의 기존 정책별 실행 조건을 변경하지 않는다. 이벤트·트리·증거·사용량 복구 때문에 degraded가 된 보고서는 목록을 비운다.

저널 순서대로 최대 64건, UTF-8 들여쓰기 JSON의 `observed_http` 섹션 최대 64 KiB까지 포함한다. 초과하면 `observed_http_truncated=true`를 기록한다. 항목이 있으면 summary 끝에 `브라우저/MITM 미검증 관측 N건 (검증·점수 무관)` 한 줄만 추가하고, 잘린 경우 `일부 생략`을 덧붙인다. 이벤트가 없으면 빈 목록과 false를 기록하고 summary에는 줄을 추가하지 않는다.

이 보고 기능은 신규 회귀 테스트 9개와 기존 관련 검사 10개, 총 19개로 확인했다. verified 필드 불변, 정책 독립 노출, artifact 비열람, degraded 빈 목록, URL 자격정보 제거, 개수·바이트 제한, 과거 저널 재생·해시 보존을 검사했다. 전체 테스트와 브라우저 검증은 실행하지 않았다.

## 검증과 경계

실제 Windows에서 로컬 HTTP/TLS 서버와 Python 명령을 사용했다. 전체 테스트 스위트·브라우저 실행·UI 검증·외부 공격 대상 요청은 하지 않았다.

- 평문 프록시 테스트 15개: 청크·트레일러·Expect 100, keep-alive, 교차 출처, 늦은 응답과 pipeline exec 결합, 기록 실패/지연, 한도 초과, CONNECT 첫 연결에서 원래 서버 인증서 유지.
- 별도 MITM 환경 테스트 10개: 실제 로컬 TLS, 최초 인증서 거부 후 다음 연결 우회, 실행 결합, 수집 실패, 큰 응답, 교차 출처, 불투명 binding 및 제어 실패 시 해제.
- 브라우저 감사 수집 이벤트 단위 테스트 11개: 응답 body 출처, POST 응답 비재실행, 메타데이터·body 한도, 세대별 exec 결합, 취소 및 지연 응답 제외. 실제 브라우저는 실행하지 않는다.
- 브라우저 감사 런타임 테스트 4개: 별도 source/라벨 저장, 저널과 artifact SHA 검증, 감사 자료가 검증·점수·보고로 승격되지 않음, 감사 파일 저장 실패 시 실행 유지, source 위장·다른 origin 제외. 관련 기존 검사 4개를 포함해 이번 마무리의 단위 테스트 19개가 통과했다.
- 런타임 테스트: 직접 urllib 수집→supported 관측→미검증 형제보다 높은 순위→rebuild/보고, 헬퍼 중복 방지, 증거 변조 거부, 수집 초기화·파일 저장 실패 격리, 실행 중 ID 조회, 기본 CA 미생성, MITM 별도 감사 저장 및 장애 시 직접 연결 복귀.
- 기존 관측·저널·일반 명령 관련 42개 테스트 통과. 과거 저널 입력·컨텍스트 해시와 시드 수집 재생 검사 포함.
- v4 루프 관련 테스트 통과. 원본 파서가 지원하지 않는 HTTP/2는 불투명 바이트 전달만 확인했으며, HTTP/2 전체 서버 동작이나 HTTP/2 검증 증거 수집을 검증했다고 주장하지 않는다.

observation_verification.py, risk_rubric.py, tree.py, 기존 http_capture.py/http_capture_client.py의 바이트 해시는 작업 시작 시점과 동일하다. reporting.py는 별도 observed_http 키와 summary의 라벨된 한 줄만 추가했으며 verified 통계 계산은 수정하지 않았다. v5.md에도 변경이 없다.
