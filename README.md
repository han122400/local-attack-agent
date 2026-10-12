# Local Attack Agent

로컬 Ollama 모델이 OWASP Juice Shop의 화면과 네트워크 관찰을 바탕으로 탐색 계획과 다음 행동을 선택하는 실험 프로젝트입니다. FastAPI 대시보드에서 계획, 모델 출력, 관찰 기록, 가설과 응답 비교 결과를 확인할 수 있습니다.

현재 구현의 대상은 `http://127.0.0.1:3000/`으로 제한됩니다. 응답 차이는 관찰 결과로 기록하며 취약점 확정을 의미하지 않습니다.
브라우저의 WebSocket 연결은 모두 차단하고 HTTP 조회만 사용합니다. 실행은 최대 10분이며,
응답 본문 수집은 최대 8초입니다. 수집 실패 응답은 오류로 기록하고 실험 후보에서 제외합니다.

## 실행 환경

- Python 3.10 이상
- Docker Desktop
- Ollama 및 설치된 로컬 모델
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

## 구성

- `app.py`: FastAPI API와 실행 관리
- `explorer.py`: 브라우저 관찰, LLM 계획 실행, 가설 실험 및 응답 비교
- `planning.py`: 계획 스키마, 모델 지시문, 결정 검증
- `lab.py`: 로컬 Juice Shop 컨테이너 상태 확인과 시작
- `static/`: 대시보드
- `tests/`: 테스트
- `Version/`: v1·v2 설계 문서

## 개발 상태

MVP 실험 단계입니다. 검색 입력은 연결된 레이블, 접근성 이름과 상위 검색 영역을 함께 읽어 인식하며, 숨겨지거나 접힌 입력은 선택지에서 제외합니다.
계획, 탐색 순서와 검색값은 LLM이 선택합니다. 실행기는 화면 상태별 실패·변화 없는 행동과 반복 전환을 제한하고 그 이유를 모델에 전달합니다. 같은 입력에서 다른 검색값은 선택할 수 있습니다.
실행할 수 없는 선택이 3회 연속 이어지면 `탐색 정체`로 종료하며, 모델 연결·응답 생성 오류는 별도로 표시합니다. 실행 데이터, 로그, 로컬 의존성은 Git 저장소에서 제외됩니다.
