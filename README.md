# Local Attack Agent

로컬 Ollama 또는 OpenRouter API 모델이 OWASP Juice Shop의 화면과 네트워크 관찰을 바탕으로 탐색 계획과 다음 행동을 선택하는 실험 프로젝트입니다. FastAPI 대시보드에서 계획, 모델 출력, 관찰 기록, 가설과 응답 비교 결과를 확인할 수 있습니다.

기본 대상은 `http://127.0.0.1:3000/`의 Juice Shop입니다. 새 실험 화면에서 본인이 소유하거나 탐색 권한이 있는 사이트의 HTTP(S) URL을 직접 입력하고, 설정 저장 버튼으로 다음 방문에도 유지할 수 있습니다. 각 실험의 Playwright 페이지 요청과 기본 HTTP 도우미는 선택한 대상의 origin으로 제한됩니다. Juice Shop이 아닌 대상은 연결을 선확인하거나 자동 초기화하지 않습니다. 응답 차이는 관찰 결과로 기록하며 취약점 확정을 의미하지 않습니다.
브라우저의 WebSocket 연결은 모두 차단하고 HTTP 조회만 사용합니다. 실행은 최대 10분이며,
응답 본문 수집은 최대 8초입니다. 수집 실패 응답은 오류로 기록하고 실험 후보에서 제외합니다.

## 실행 환경

- Python 3.10 이상
- Docker Desktop
- Ollama 및 설치된 로컬 모델 또는 OpenRouter API 키
- Playwright Chromium

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

테스트는 Windows에서 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`,
macOS/Linux에서 `.venv/bin/python -m unittest discover -s tests -v`로 실행합니다.
브라우저 테스트에는 위에서 설치한 Chromium이 필요하며, 모델 응답과 사이트는 모의 데이터로 검증합니다.

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
계획, 탐색 순서와 검색값은 LLM이 선택합니다. 실행기는 화면 상태별 실패·변화 없는 행동과 반복 전환을 제한하고 그 이유를 모델에 전달합니다. 같은 입력에서 다른 검색값은 선택할 수 있습니다.
실행할 수 없는 선택이 3회 연속 이어지면 `탐색 정체`로 종료하며, 모델 연결·응답 생성 오류는 별도로 표시합니다. 실행 데이터, 로그, 로컬 의존성은 Git 저장소에서 제외됩니다.
