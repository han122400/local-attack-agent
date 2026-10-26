# Local Attack Agent

로컬 Ollama 또는 OpenRouter API 모델이 OWASP Juice Shop의 화면과 네트워크 관찰을 바탕으로 탐색 계획과 다음 행동을 선택하는 실험 프로젝트입니다. FastAPI 대시보드에서 계획, 모델 출력, 관찰 기록, 가설과 응답 비교 결과를 확인할 수 있습니다.

기본 대상은 `http://127.0.0.1:3000/`의 Juice Shop입니다. 새 실험 화면에서 본인이 소유하거나 탐색 권한이 있는 사이트의 HTTP(S) URL을 직접 입력하고, 설정 저장 버튼으로 다음 방문에도 유지할 수 있습니다. 각 실험의 Playwright 페이지 요청과 기본 HTTP 도우미는 선택한 대상의 origin으로 제한됩니다. Juice Shop이 아닌 대상은 연결을 선확인하거나 자동 초기화하지 않습니다. 응답 차이는 관찰 결과로 기록하며 취약점 확정을 의미하지 않습니다.
브라우저의 WebSocket 연결은 모두 차단하고 HTTP 조회만 사용합니다. 기본 실행 예산은 20분이며,
응답 본문 수집은 최대 8초입니다. 수집 실패 응답은 오류로 기록하고 실험 후보에서 제외합니다.

## 실행 환경

- Python 3.10 이상
- Docker Desktop (로컬 Juice Shop 실습 타깃을 사용할 때만 필요)
- Ollama 및 설치된 로컬 모델 또는 OpenRouter API 키
- Playwright Chromium

AI가 작성한 코드와 Playwright 브라우저 워커는 main과 동일하게 로컬 Python·셸에서 실행합니다.
별도의 에이전트 실행 이미지 빌드나 WSL은 필요하지 않습니다. Docker는 Juice Shop 타깃 실행에만 사용합니다.

## 시작하기

프로젝트 폴더에서 Python 의존성과 브라우저를 설치합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

macOS/Linux에서는 다음 명령을 사용합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

Juice Shop 실습 컨테이너를 준비합니다.

```powershell
docker pull bkimminich/juice-shop:v20.2.0
docker run -d --name juice-lab -p 127.0.0.1:3000:3000 bkimminich/juice-shop:v20.2.0
```

컨테이너가 이미 있다면 `docker start juice-lab`으로 시작합니다. Ollama를 실행하고 `ollama list`에 모델이 있는지 확인한 뒤 대시보드를 시작합니다.

```powershell
.\.venv\Scripts\python.exe app.py
```

macOS/Linux에서는 `.venv/bin/python app.py`로 시작합니다.

관련 단위 테스트는 모듈을 지정해 실행합니다(예: `python -m unittest discover -s tests -p test_local_runtime.py -v`).
전체 테스트나 브라우저 검증은 명시적으로 요청한 경우에만 실행합니다.

브라우저에서 <http://127.0.0.1:8765/>에 접속해 설치된 모델을 선택합니다. Ollama 연결 주소는 `http://127.0.0.1:11434`입니다.

### OpenRouter 모델 사용

프로젝트 루트의 `.env`에 키를 직접 입력합니다. 형식은 `.env.example`을 참고하세요.

```dotenv
OPENROUTER_API_KEY=여기에_발급받은_키
```

의존성을 설치한 뒤 서버를 재시작하고, 새 실험 화면에서 **모델 제공자 → OpenRouter API**를 선택합니다.
API에서 조회한 텍스트 생성 모델 중 하나를 선택해 실행할 수 있습니다. Ollama를 실행하지 않아도 됩니다.
키를 수정한 경우 모델 목록의 **새로고침**을 누르면 다시 읽습니다. 같은 이름의 프로세스 환경변수가 있으면 `.env`보다 우선합니다.

목표, 관찰 내용, 코드와 실행 결과가 OpenRouter 및 해당 모델 제공자에게 전송되며 모델별 요금이 적용됩니다.
키는 서버의 인증 헤더에만 넣고 API 응답·세션 로그·하위 실행 프로세스 환경에는 넣지 않습니다. `.env`는 Git에서 제외됩니다.

Thinking과 Temperature는 모델이 지원할 때만 적용합니다. 컨텍스트 토큰은 앱의 대화 압축 예산이며
원격 모델의 컨텍스트 크기를 변경하지 않습니다. 출력 토큰은 `max_tokens`로 전달하되,
모델의 한도와 입력 분량 추정치에 따라 상한을 줄입니다. 토크나이저별 실제 입력 한도는 제공자가 최종 검증합니다.
구조화 출력을 지원하는 모델에는 JSON Schema를, JSON 모드만 지원하면 JSON 모드를 요청하며,
그 외 모델도 기존 프롬프트와 명령 검증·재시도 흐름을 사용합니다.
인증 실패, 크레딧 부족, 호출 제한은 화면과 로그에 오류로 표시하며 다른 모델로 자동 전환하지 않습니다.

## 구성

- `app.py`: FastAPI API와 실행 관리
- `explorer.py`: 브라우저 관찰, LLM 계획 실행, 가설 실험 및 응답 비교
- `model_providers.py`: OpenRouter 모델 목록, 설정 변환 및 스트리밍 어댑터
- `planning.py`: 계획 스키마, 모델 지시문, 결정 검증
- `lab.py`: 로컬 Juice Shop 컨테이너 상태 확인과 시작
- `static/`: 대시보드
- `tests/`: 테스트
- `Version/`: v1·v2 설계 문서

## 개발 상태

MVP 실험 단계입니다. 검색 입력은 연결된 레이블, 접근성 이름과 상위 검색 영역을 함께 읽어 인식하며, 숨겨지거나 접힌 입력은 선택지에서 제외합니다.
입력칸과 결과 선택자가 확인되면 모델은 `await compare_inputs(index, normal_value, test_value,
result_selector)`로 정상 입력·테스트 입력·정상 입력 재확인을 한 명령에서 수집할 수 있습니다.
기존 `submit_input`을 재사용하며, 각 단계에서 입력칸의 식별 정보가 유지되는지 확인합니다.
결과는 `status`, `comparison`, `samples`로 반환합니다. 입력칸 변경·제출 실패·모든 결과가 비어
있음·정상 결과 재확인의 불일치는 `inconclusive`입니다. 비교 범위는 결과 수와 앞 5개 텍스트
샘플이며, `compared`도 취약점 확정이나 전체 결과 동일성을 뜻하지 않습니다. 로그인 상태 등 다른
조건의 변화는 별도로 확인해야 합니다. 결과가 읽히기 전에 실행 제한 시간이 끝나면 기존 타임아웃
처리를 따릅니다. 정상→테스트 사이의 개별 판단이 필요하면 기존 헬퍼를 그대로 사용할 수 있습니다.

실모델의 명령 생성률·시간·추론량과 제한된 오프라인 항목 완료율은
[`benchmarks/harness_policy_eval.py`](benchmarks/harness_policy_eval.py)로 측정합니다. 실행 방법과
지표의 한계는 [`benchmarks/README.md`](benchmarks/README.md)를 참고하세요.

계획, 탐색 순서와 검색값은 LLM이 선택하고 `finish`는 전체 실행을 종료합니다. 최근 실행이 실패하면
다음 입력에 실패 단계·동작·오류 원인을 연결해 전달합니다. 동일 명령과 동일 결과가 연속될 때만
반복 경고를 주며, 서로 다른 입력의 같은 출력이나 중간 준비 작업을 거친 재실행은 반복으로
단정하지 않습니다. 이 경고로 자동 이동·종료하지 않습니다.

모델에 전달할 긴 명령·코드·출력·오류는 줄바꿈과 앞뒤 내용을 유지한 미리보기로 줄입니다.
기존 기록 압축에 최근 실행 상세와 오류 피드백도 포함하며, 실제 전송할 최초 문맥 크기를 다시
검사합니다. 목표·대상·파일 경로는 임의로 자르지 않고, 필수 내용이 문자 예산에 들어가지 않으면
호출 전 오류를 표시합니다. 문자 예산은 토큰 수의 근사치이며, Harness 내부 재시도 메시지에도 별도의 입력 크기 제한을 적용합니다. 실행 명령과 저장된 findings는 미리보기로 바꾸지 않습니다.
모델 연결·응답 생성 오류는 별도로 표시하며, 실행 데이터, 로그, 로컬 의존성은 Git 저장소에서 제외됩니다.
