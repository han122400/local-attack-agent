"""Status and explicit startup for the existing local Juice Shop container."""

import json
import subprocess
import threading
import time
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


def _probe_initial_state() -> dict:
    """Check reset data, not only server reachability or container mounts."""
    with urlopen(TARGET + "rest/products/search?q=", timeout=5) as response:
        products = json.load(response)
    with urlopen(TARGET + "api/Challenges/", timeout=5) as response:
        challenges = json.load(response)
    product_rows = products.get("data") if isinstance(products, dict) else None
    challenge_rows = challenges.get("data") if isinstance(challenges, dict) else None
    if not isinstance(product_rows, list) or not isinstance(challenge_rows, list):
        raise RuntimeError("Juice Shop 초기 데이터 형식이 올바르지 않습니다.")
    solved = sum(bool(item.get("solved")) for item in challenge_rows if isinstance(item, dict))
    verified = (products.get("status") == "success" and challenges.get("status") == "success"
                and len(product_rows) == 46 and len(challenge_rows) == 116 and solved == 0
                and any(item.get("name") == "Apple Juice (1000ml)" for item in product_rows if isinstance(item, dict)))
    return {"verified": verified, "products": len(product_rows), "challenges": len(challenge_rows),
            "solved": solved, "apple_juice_present": any(item.get("name") == "Apple Juice (1000ml)"
                                                   for item in product_rows if isinstance(item, dict))}


def reset_lab(*, wait_seconds: float = 45) -> dict:
    """Restart the existing fixed container and verify its reseeded data."""
    with _start_lock:
        try:
            inspected = _docker("inspect", CONTAINER, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("juice-lab 설정을 확인할 수 없습니다.") from exc
        if inspected.returncode:
            raise RuntimeError("기존 juice-lab 컨테이너를 찾을 수 없습니다.")
        try:
            items = json.loads(inspected.stdout)
            info = items[0]
            image = info["Config"]["Image"]
            mounts = info["Mounts"]
            published = info["NetworkSettings"]["Ports"]["3000/tcp"]
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise RuntimeError("juice-lab 설정 형식을 확인할 수 없습니다.") from exc
        if (image != "bkimminich/juice-shop:v20.2.0" or mounts != []
                or not any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == "3000"
                           for item in published)):
            raise RuntimeError("juice-lab 이미지·마운트·포트가 예상한 실험환경과 다릅니다.")
        try:
            restarted = _docker("restart", CONTAINER, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("juice-lab 재시작 결과를 확인하지 못했습니다.") from exc
        if restarted.returncode:
            raise RuntimeError("juice-lab 재시작에 실패했습니다.")
        deadline = time.monotonic() + max(0, wait_seconds)
        last_error = ""
        while True:
            try:
                probe = _probe_initial_state()
                if probe.get("verified"):
                    return {"container": CONTAINER, "image": image, "verified": True,
                            "probe": probe, "checked_at": time.time()}
                last_error = f"초기 데이터 불일치: {probe}"
            except (OSError, ValueError, RuntimeError) as exc:
                last_error = str(exc)[:300]
            if time.monotonic() >= deadline:
                raise RuntimeError(f"juice-lab 초기화 검증 실패: {last_error}")
            time.sleep(min(0.5, deadline - time.monotonic()))
