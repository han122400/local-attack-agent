# Local Attack Agent

로컬 Ollama 또는 OpenRouter API 모델이 OWASP Juice Shop의 화면과 네트워크 관찰을 바탕으로 탐색 계획과 다음 행동을 선택하는 실험 프로젝트입니다. FastAPI 대시보드에서 계획, 모델 출력, 관찰 기록, 가설과 응답 비교 결과를 확인할 수 있습니다.

현재 구현의 대상은 `http://127.0.0.1:3000/`으로 제한됩니다. 응답 차이는 관찰 결과로 기록하며 취약점 확정을 의미하지 않습니다.
브라우저의 WebSocket 연결은 모두 차단하고 HTTP 조회만 사용합니다. 기본 실행 예산은 20분이며,
응답 본문 수집은 최대 8초입니다. 수집 실패 응답은 오류로 기록하고 실험 후보에서 제외합니다.

## 실행 환경

- Python 3.10 이상
- Docker Desktop
- Ollama 및 설치된 로컬 모델 또는 OpenRouter API 키
- Playwright Chromium

컨트롤러는 현재 macOS/Linux에서 실행합니다. 생성 코드가 바꾼 심볼릭 링크를 따라 호스트 파일에
접근하지 않도록 POSIX 디렉터리 핸들 기반 파일 접근을 사용하며, 지원되지 않는 환경에서는 거부합니다.
Windows에서는 WSL2의 Linux 환경에서 컨트롤러를 실행해야 합니다.

## 시작하기

Docker Desktop을 켜고 프로젝트 폴더에서 **격리 실행 이미지**를 한 번 빌드합니다.
Dockerfile만 빌더에 전달하므로 프로젝트 파일, `.env`, 모델이나 실행 로그는 이미지에 들어가지 않습니다.

```bash
docker build -t local-attack-agent-runtime:v3 - < Dockerfile.runtime
```

`run_command`와 `run_python`은 이 이미지의 컨테이너 안에서만 실행됩니다. 이미지 또는 Docker가
없으면 실행을 거부하며 호스트 Python/셸로 대체 실행하지 않습니다. 컨테이너에는 세션 runtime만
`/work`로 연결하고, 호스트 API 키·Docker 소켓·프로젝트 폴더는 연결하지 않습니다.
네트워크는 `none`, 루트 파일시스템은 읽기 전용이며 CPU·메모리·프로세스 수를 제한합니다.
각 실행에는 전용 컨테이너 소유권을 기록하고, 종료·취소·복구 때 해당 컨테이너도 정리합니다.

모든 생성 명령은 호스트 OS와 관계없이 **Linux 셸** 명령입니다(`cat`, `rm`, `python` 등).
셸은 runtime 파일 분석에 사용합니다. 사이트 조회에는 `http_get`/`get_text`/`get_json` 또는
브라우저 탐색을 사용하세요. 이 경로만 컨트롤러가 허용된 타깃의 GET/HEAD 요청으로 중계합니다.
컨테이너 내부 `curl`·`urllib`·`context.request` 등의 직접 네트워크 연결은 사용할 수 없습니다.

프로젝트 폴더에서 컨트롤러의 Python 의존성을 설치합니다(macOS/Linux 또는 WSL2).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Juice Shop 실습 컨테이너를 준비합니다.

```bash
docker pull bkimminich/juice-shop:v20.2.0
docker run -d --name juice-lab -p 127.0.0.1:3000:3000 bkimminich/juice-shop:v20.2.0
```

컨테이너가 이미 있다면 `docker start juice-lab`으로 시작합니다. Ollama를 실행하고 `ollama list`에 모델이 있는지 확인한 뒤 대시보드를 시작합니다.

```bash
.venv/bin/python app.py
```

테스트는 필요한 모듈을 지정해 실행합니다(예: `.venv/bin/python -m unittest discover -s tests -p test_security_regressions.py -v`).
실제 실행기 테스트에는 위에서 빌드한 Docker 이미지가 필요합니다. UI 테스트를 명시적으로 실행할 때만
호스트에 `.venv/bin/python -m playwright install chromium`으로 Chromium을 추가로 설치하세요.

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
- `process_sandbox.py`, `Dockerfile.runtime`: 생성 코드의 Docker 격리와 소유 컨테이너 정리
- `http_evidence.py`: 타깃 제한 HTTP 중계 및 컨트롤러가 보존한 요청·응답 증거
- `context_limits.py`: 최근 실행 결과와 재시도 메시지를 포함한 입력 크기 제한
- `lab.py`: 로컬 Juice Shop 컨테이너 상태 확인과 시작
- `static/`: 대시보드
- `tests/`: 테스트
- `Version/`: v1·v2 설계 문서

## 개발 상태

출력 문자열, 모델의 주장, 생성 파일은 그 자체로 검증된 HTTP 증거가 아닙니다. 컨트롤러가
실제로 수집한 요청·응답 아티팩트의 ID와 해시를 각 단계에 연결하며 보고서는 이 응답만
‘확인된 관찰’에 표시합니다. 모델 종료 요약은 미검증 주장으로 따로 표시합니다.
HTTP 증거 없이 `finish`하면 결과는 `inconclusive`이고 점수는 0입니다. HTTP 관찰이 있는
`completed`도 취약점 확정이나 목표 전체 달성을 보증하지 않습니다.
`scorecard.py`의 점수는 HTTP 관찰 수집 지표이며 취약점 탐지 정확도가 아닙니다.

컨텍스트는 시스템 프롬프트와 출력 여유를 제외한 문자 예산으로 제한합니다. 최근 명령·출력과
재시도 피드백까지 포함해 다시 검사하며, 최소한의 필수 입력조차 맞지 않으면 모델을 호출하지
않습니다. 문자 수는 토큰 수의 추정치이며 모델별 정확한 토큰 계산은 아닙니다.

MVP 실험 단계입니다. 검색 입력은 연결된 레이블, 접근성 이름과 상위 검색 영역을 함께 읽어 인식하며, 숨겨지거나 접힌 입력은 선택지에서 제외합니다.
계획, 탐색 순서와 검색값은 LLM이 선택합니다. 실행기는 화면 상태별 실패·변화 없는 행동과 반복 전환을 제한하고 그 이유를 모델에 전달합니다. 같은 입력에서 다른 검색값은 선택할 수 있습니다.
실행할 수 없는 선택이 3회 연속 이어지면 `탐색 정체`로 종료하며, 모델 연결·응답 생성 오류는 별도로 표시합니다. 실행 데이터, 로그, 로컬 의존성은 Git 저장소에서 제외됩니다.
