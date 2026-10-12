"""Status and explicit startup for the existing local Juice Shop container."""

import json
import subprocess
import threading
from urllib.request import urlopen

CONTAINER = "juice-lab"
TARGET = "http://127.0.0.1:3000/"
_start_lock = threading.Lock()


def _docker(*args: str, timeout: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _web_ready() -> bool:
    try:
        with urlopen(TARGET, timeout=1) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def _state(status: str, message: str, can_start: bool = False) -> dict:
    return {"status": status, "message": message, "can_start": can_start,
            "ready": status == "ready", "container": CONTAINER, "url": TARGET}


def get_status() -> dict:
    try:
        result = _docker("inspect", "--format", "{{json .State}}", CONTAINER)
    except (OSError, subprocess.TimeoutExpired):
        return _state("unavailable", "Docker 연결 안 됨 · Docker Desktop 실행을 확인하세요.")
    if result.returncode:
        if "no such" in result.stderr.lower():
            return _state("missing", "juice-lab 컨테이너가 없습니다. 실험환경을 먼저 설치하세요.")
        return _state("unavailable", "Docker 연결 안 됨 · Docker Desktop 실행을 확인하세요.")
    try:
        state = json.loads(result.stdout)
        container_status = state["Status"]
    except (ValueError, KeyError, TypeError):
        return _state("error", "컨테이너 상태를 확인할 수 없습니다.")
    if container_status == "running":
        if _web_ready():
            return _state("ready", "정상 · Juice Shop에 연결할 수 있습니다.")
        return _state("starting", "실행 중 · 3000 포트의 웹 응답을 기다리는 중입니다.")
    if container_status == "restarting":
        return _state("starting", "컨테이너 재시작 중입니다.")
    if container_status in {"exited", "created", "dead"}:
        return _state("stopped", "꺼짐 · 실험환경 켜기를 눌러 시작하세요.", can_start=True)
    return _state("error", f"컨테이너 상태: {container_status} · Docker Desktop에서 확인하세요.")


def start_lab() -> dict:
    with _start_lock:
        state = get_status()
        if state["status"] in {"ready", "starting"}:
            return state
        if not state["can_start"]:
            raise RuntimeError(state["message"])
        try:
            result = _docker("start", CONTAINER, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("시작 결과를 확인하지 못했습니다. 상태를 새로 확인하세요.") from exc
        if result.returncode:
            raise RuntimeError("컨테이너 시작 실패 · Docker Desktop에서 juice-lab의 로그와 포트를 확인하세요.")
        return get_status()
