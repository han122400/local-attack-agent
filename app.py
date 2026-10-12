"""Local, read-only Juice Shop explorer with an Ollama-driven dashboard."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool


TARGET = "http://127.0.0.1:3000/"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:2b"
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765
MAX_STEPS = 20
MAX_CANDIDATES = 150
STATIC_DIR = Path(__file__).resolve().parent / "static"


def normalize_url(raw: str, source: str = TARGET) -> str | None:
    """Resolve a link while enforcing the one permitted origin."""
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        return None
    if re.search(r"[\x00-\x20\x7f\\]", raw):
        return None
    try:
        parsed = urlsplit(urljoin(source, raw))
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 3000
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return urlunsplit(("http", "127.0.0.1:3000", parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def query_inputs(url: str) -> list[str]:
    return sorted({key for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)})


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.inputs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append((values["href"], "page"))
        elif tag == "script" and values.get("src"):
            self.links.append((values["src"], "script"))
        elif tag == "form" and values.get("action"):
            self.links.append((values["action"], "form"))
        elif tag in {"input", "textarea", "select"} and values.get("name"):
            self.inputs.append(values["name"])


JS_PATH = re.compile(r"""[`"']((?:/|\\/)(?:api/|rest/|assets/|login\b|search\b|products\b|score-board\b)[^`"'\s]{0,140})[`"']""")
JS_ROUTE = re.compile(r"""\bpath\s*:\s*[`"']([a-z][a-z0-9-]{1,60})[`"']""", re.IGNORECASE)


def extract_candidates(body: str, content_type: str, source: str) -> tuple[list[dict], list[str]]:
    links: list[tuple[str, str]] = []
    inputs: list[str] = []
    if "html" in content_type:
        parser = LinkParser()
        parser.feed(body)
        links.extend(parser.links)
        inputs.extend(parser.inputs)
    elif "javascript" in content_type or source.endswith(".js"):
        for match in JS_PATH.finditer(body[:2_000_000]):
            path = match.group(1).replace("\\/", "/")
            if "${" not in path:
                links.append((path, "api" if path.lstrip("/").startswith(("api/", "rest/")) else "page"))
        for match in JS_ROUTE.finditer(body[:2_000_000]):
            links.append(("/" + match.group(1), "route"))
    found: list[dict] = []
    seen: set[str] = set()
    for raw, kind in links:
        url = normalize_url(raw, source)
        if url and url not in seen:
            seen.add(url)
            found.append({"url": url, "kind": kind, "source": source, "inputs": query_inputs(url)})
    return found, sorted(set(inputs))


@dataclass
class FetchResult:
    status: int
    content_type: str
    body: str
    body_hash: str
    elapsed: float
    error: str = ""


def fetch_with_curl(url: str) -> FetchResult:
    """Run a fixed curl command without passing model text through a shell."""
    if normalize_url(url) != url:
        raise ValueError("대상 범위를 벗어난 URL")
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="juice-explorer-") as temp:
        headers = Path(temp) / "headers.txt"
        body_file = Path(temp) / "body.bin"
        args = [
            "curl.exe", "--silent", "--show-error", "--noproxy", "*",
            "--proto", "=http", "--max-time", "8",
            "--max-filesize", "2000000", "--dump-header", str(headers),
            "--output", str(body_file), "--write-out", "%{http_code}",
            "--url", url,
        ]
        try:
            process = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return FetchResult(0, "", "", "", time.monotonic() - start, str(exc))
        header_text = headers.read_text(encoding="iso-8859-1", errors="replace") if headers.exists() else ""
        content_type = ""
        for line in header_text.splitlines():
            if line.lower().startswith("content-type:"):
                content_type = line.split(":", 1)[1].strip().split(";", 1)[0].lower()
        data = body_file.read_bytes() if body_file.exists() else b""
        status = int(process.stdout.strip()) if process.stdout.strip().isdigit() else 0
        error = process.stderr.strip()[:300] if process.returncode else ""
        return FetchResult(
            status, content_type, data.decode("utf-8", errors="replace"),
            hashlib.sha256(data).hexdigest() if data else "",
            round(time.monotonic() - start, 3), error,
        )


class StopRequested(Exception):
    """Raised between streamed chunks when the user stops the run."""


def stream_ollama(
    payload: dict,
    on_delta: Callable[[str, str], None],
    stop_requested: Callable[[], bool],
) -> tuple[str, str, str]:
    """Read Ollama NDJSON incrementally, preserving its thinking/content split."""
    request = UrlRequest(OLLAMA_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    thoughts: list[str] = []
    contents: list[str] = []
    done_reason = ""
    with urlopen(request, timeout=45) as response:
        for line in response:
            if stop_requested():
                raise StopRequested()
            if not line.strip():
                continue
            chunk = json.loads(line)
            message = chunk.get("message") or {}
            for channel, pieces in (("thinking", thoughts), ("content", contents)):
                delta = message.get(channel) or ""
                if delta:
                    pieces.append(delta)
                    on_delta(channel, delta)
            if chunk.get("done"):
                done_reason = chunk.get("done_reason") or ""
    return "".join(thoughts), "".join(contents), done_reason


def choose_with_ollama(
    candidates: list[dict],
    recent: list[dict],
    step: int,
    on_text: Callable[[str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[int | None, str, bool, str]:
    """Stream visible model output with thinking disabled, then parse its choice."""
    shown = candidates[:12]
    prompt = {
        "goal": "Explore local OWASP Juice Shop. Choose exactly one candidate ID to visit. JavaScript bundles can reveal application routes and API paths. Do not stop while candidates exist.",
        "step": step,
        "max_steps": MAX_STEPS,
        "recent": recent[-5:],
        "candidates": [{"id": i, "url": item["url"], "kind": item["kind"]} for i, item in enumerate(shown)],
    }
    schema = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "action": {"type": "string", "enum": ["visit"]},
            "candidate_id": {"type": "integer"},
        },
        "required": ["action", "candidate_id", "reason"],
    }
    emit = on_text or (lambda delta: None)
    stopped = stop_requested or (lambda: False)
    payload = {
        "model": MODEL,
        "stream": True,
        "think": False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 220},
        "messages": [
            {"role": "system", "content": "You schedule HTTP exploration of a local test app. Reply in JSON only. Choose a listed candidate ID. Never stop while candidates remain. Put a brief Korean explanation in reason before the choice. Explain why the URL helps map the app. Do not claim vulnerabilities or attacks."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    last_error = ""
    final_content = ""
    for attempt in range(2):
        try:
            if attempt:
                emit("\n[모델 응답 재시도]\n")
            _, final_content, _ = stream_ollama(
                payload,
                lambda channel, delta: emit(delta) if channel == "content" else None,
                stopped,
            )
            if not final_content:
                raise ValueError("모델 출력이 비었습니다.")
            answer = json.loads(final_content)
            action = answer.get("action")
            reason = str(answer.get("reason", ""))[:180]
            candidate_id = answer.get("candidate_id")
            if action == "visit" and isinstance(candidate_id, int) and 0 <= candidate_id < len(shown):
                return candidate_id, reason or "모델이 다음 후보를 선택했습니다.", False, final_content
            last_error = "모델이 유효하지 않은 후보 번호를 반환했습니다."
        except StopRequested:
            return None, "사용자가 탐색을 중지했습니다.", False, final_content
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, OSError) as exc:
            last_error = str(exc)[:180]
    return 0, f"모델 응답 오류로 첫 후보 선택: {last_error}", True, final_content


def check_ollama() -> None:
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as response:
            models = json.load(response).get("models", [])
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Ollama 연결 실패: {exc}") from exc
    if not any(item.get("name") == MODEL for item in models):
        raise RuntimeError(f"Ollama 모델 {MODEL}을 찾지 못했습니다.")


def is_confirmed(result: FetchResult, kind: str, root_hash: str, is_root: bool) -> tuple[bool, bool]:
    same_as_root = not is_root and bool(root_hash) and result.body_hash == root_hash and "html" in result.content_type
    confirmed = (
        result.status in range(200, 400)
        and not same_as_root
        and not result.error
        and (kind != "api" or "json" in result.content_type)
    ) or (result.status in {401, 403} and "json" in result.content_type)
    return bool(confirmed), same_as_root


class Explorer:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.subscribers: set[queue.Queue] = set()
        self.state: dict = self._fresh_state()

    @staticmethod
    def _fresh_state() -> dict:
        return {
            "status": "idle", "target": TARGET, "model": MODEL, "max_steps": MAX_STEPS,
            "step": 0, "started_at": None, "finished_at": None, "message": "시작을 누르면 탐색합니다.",
            "events": [], "discoveries": [], "errors": 0, "fallbacks": 0,
            "console": "", "model_streaming": False,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state, ensure_ascii=False))

    def subscribe(self) -> tuple[queue.Queue, str]:
        subscriber: queue.Queue = queue.Queue(maxsize=1024)
        with self.lock:
            self.subscribers.add(subscriber)
            return subscriber, self.state["console"]

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.lock:
            self.subscribers.discard(subscriber)

    def _append_console(self, chunk: str) -> None:
        with self.lock:
            self.state["console"] = (self.state["console"] + chunk)[-60000:]
            for subscriber in self.subscribers:
                if subscriber.full():
                    subscriber.get_nowait()
                subscriber.put_nowait(("delta", chunk))

    def start(self) -> bool:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            self.stop_event.clear()
            self.state = self._fresh_state()
            for subscriber in self.subscribers:
                if subscriber.full():
                    subscriber.get_nowait()
                subscriber.put_nowait(("snapshot", ""))
            self.state.update(status="running", started_at=time.time(), message="탐색 준비 중")
            self._append_console("Juice Explorer v1\n대상: http://127.0.0.1:3000/ · 모델: qwen3.5:2b · think:false\n\n")
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()
            return True

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.state["status"] == "running":
                self.state["status"] = "stopping"
                self.state["message"] = "현재 요청이 끝나면 중지합니다."

    def _run(self) -> None:
        pending: dict[str, dict] = {TARGET: {"url": TARGET, "kind": "page", "source": "start"}}
        visited: set[str] = set()
        recent: list[dict] = []
        root_hash = ""
        try:
            check_ollama()
            for step in range(1, MAX_STEPS + 1):
                if self.stop_event.is_set():
                    break
                options = sorted(pending.values(), key=lambda item: (self._priority(item), item["url"]))[:12]
                if not options:
                    break
                model_output = ""
                if step <= 2:
                    selection, reason, fallback = 0, "시작 URL 확인" if step == 1 else "앱 경로를 찾기 위한 스크립트 확인", False
                else:
                    with self.lock:
                        self.state["message"] = "Ollama가 다음 경로를 선택하는 중"
                        self.state["model_streaming"] = True
                    self._append_console(f"[OLLAMA] 단계 {step} · 다음 경로 선택\n")
                    try:
                        selection, reason, fallback, model_output = choose_with_ollama(
                            options, recent, step,
                            on_text=self._append_console,
                            stop_requested=self.stop_event.is_set,
                        )
                    finally:
                        with self.lock:
                            self.state["model_streaming"] = False
                    self._append_console("\n")
                    if selection is None:
                        with self.lock:
                            self.state["message"] = reason
                        break
                chosen = options[selection]
                url = chosen["url"]
                del pending[url]
                if url in visited:
                    continue
                visited.add(url)
                with self.lock:
                    self.state["message"] = f"요청 중: {url}"
                self._append_console(f'[CMD] curl.exe -i --max-time 8 "{url}"\n')
                result = fetch_with_curl(url)
                self._append_console(f"[HTTP] {result.status or 'ERR'} · {result.content_type or '응답 없음'} · {result.elapsed}s\n\n")
                if step == 1 and (result.status != 200 or result.error):
                    raise RuntimeError(f"Juice Shop 연결 실패: HTTP {result.status} {result.error}")
                if step == 1:
                    root_hash = result.body_hash
                confirmed, same_as_root = is_confirmed(result, chosen["kind"], root_hash, step == 1)
                extracted, inputs = extract_candidates(result.body, result.content_type, url)
                inputs = sorted(set(inputs) | set(query_inputs(url)))
                new_count = 0
                for item in extracted:
                    if item["url"] not in visited and item["url"] not in pending and len(pending) + len(visited) < MAX_CANDIDATES:
                        pending[item["url"]] = item
                        new_count += 1
                event = {
                    "step": step, "url": url, "kind": chosen["kind"], "source": chosen["source"],
                    "command": f'curl.exe -i --max-time 8 "{url}"', "reason": reason,
                    "status": result.status, "content_type": result.content_type,
                    "elapsed": result.elapsed, "bytes": len(result.body.encode("utf-8")),
                    "new_candidates": new_count, "inputs": inputs, "confirmed": bool(confirmed),
                    "same_as_root": same_as_root, "error": result.error, "fallback": fallback,
                    "preview": result.body[:500], "model_output": model_output,
                    "at": time.time(),
                }
                recent.append({"url": url, "status": result.status, "kind": chosen["kind"], "new": new_count, "error": result.error})
                with self.lock:
                    self.state["step"] = step
                    self.state["events"].append(event)
                    discovery = {
                        "url": url, "kind": chosen["kind"], "source": chosen["source"],
                        "status": result.status, "confirmed": bool(confirmed),
                        "same_as_root": same_as_root, "inputs": inputs,
                    }
                    existing = next((item for item in self.state["discoveries"] if item["url"] == url), None)
                    if existing is None:
                        self.state["discoveries"].append(discovery)
                    else:
                        existing.update(discovery)
                    for item in extracted:
                        if not any(found["url"] == item["url"] for found in self.state["discoveries"]):
                            self.state["discoveries"].append({
                                **item, "status": None, "confirmed": False,
                                "same_as_root": False,
                            })
                    if result.error:
                        self.state["errors"] += 1
                    if fallback:
                        self.state["fallbacks"] += 1
                    self.state["message"] = f"{step}/{MAX_STEPS} 완료 · 후보 {len(pending)}개"
            with self.lock:
                self.state["status"] = "stopped" if self.stop_event.is_set() else "completed"
                self.state["finished_at"] = time.time()
                if self.state["status"] == "completed":
                    self.state["message"] = f"탐색 완료 · {self.state['step']}회 요청"
                else:
                    self.state["message"] = f"사용자가 중지했습니다 · {self.state['step']}회 요청"
            self._append_console(f"[{self.state['status'].upper()}] {self.state['message']}\n")
        except Exception as exc:  # Keep the UI usable after an unexpected worker failure.
            with self.lock:
                self.state["status"] = "error"
                self.state["finished_at"] = time.time()
                self.state["message"] = f"실행 오류: {exc}"
                self.state["errors"] += 1
                self.state["model_streaming"] = False
            self._append_console(f"[ERROR] {exc}\n")

    @staticmethod
    def _priority(item: dict) -> tuple[int, int]:
        kind = item["kind"]
        url = item["url"]
        return ({"api": 0, "page": 1, "form": 1, "route": 2, "script": 3}.get(kind, 4), 0 if url.endswith("main.js") else 1)


from explorer import V2Explorer  # noqa: E402 - keep the v1 implementation available for its regression tests.
from lab import get_status as get_lab_status, start_lab
from model_settings import settings_schema, validate_model_settings, model_presets


EXPLORER = V2Explorer()
app = FastAPI(title="Juice Explorer", docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])


@app.get("/api/state")
def get_state() -> JSONResponse:
    return JSONResponse(EXPLORER.snapshot(), headers={"Cache-Control": "no-store"})


def get_ollama_models() -> list[dict]:
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("모델 목록 응답 형식이 올바르지 않습니다.")
        models = []
        seen = set()
        for item in payload["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("모델 이름이 올바르지 않습니다.")
            if item["name"] not in seen:
                models.append({"name": item["name"], "size": item.get("size", 0)})
                seen.add(item["name"])
        return models
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Ollama 모델 목록을 불러오지 못했습니다: {exc}") from exc


@app.get("/api/models")
def ollama_models() -> JSONResponse:
    try:
        return JSONResponse({"models": get_ollama_models(), "default_model": MODEL, "settings_schema": settings_schema()}, headers={"Cache-Control": "no-store"})
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/models/details")
def ollama_model_details(model: str) -> JSONResponse:
    try:
        if model not in {item["name"] for item in get_ollama_models()}:
            raise HTTPException(status_code=400, detail="설치된 모델을 선택하세요.")
        request = UrlRequest("http://127.0.0.1:11434/api/show", data=json.dumps({"model": model}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("모델 상세 응답 형식이 올바르지 않습니다.")
        info = payload.get("model_info") or {}
        lengths = [value for key, value in info.items() if key.endswith(".context_length") and type(value) is int]
        return JSONResponse({"model": model, "capabilities": payload.get("capabilities", []),
                             "thinking": payload.get("thinking"), "context_length": min(lengths) if lengths else None,
                             "parameters": payload.get("parameters", ""), "details": payload.get("details", {}),
                             "presets": model_presets(model)}, headers={"Cache-Control": "no-store"})
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail=f"Ollama 모델 상세를 불러오지 못했습니다: {exc}") from exc


def ollama_processor(size: int | None, size_vram: int | None) -> str:
    """Match ollama ps memory-placement labels; this is not GPU utilization."""
    if type(size) is not int or type(size_vram) is not int or size < 0 or size_vram < 0:
        return "Unknown"
    if size_vram == 0:
        return "100% CPU"
    if size_vram == size:
        return "100% GPU"
    if size_vram > size or size == 0:
        return "Unknown"
    cpu_percent = int((size - size_vram) / size * 100 + 0.5)
    return f"{cpu_percent}%/{100 - cpu_percent}% CPU/GPU"


@app.get("/api/ollama/ps")
def ollama_ps() -> JSONResponse:
    try:
        with urlopen("http://127.0.0.1:11434/api/ps", timeout=4) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("실행 상태 응답 형식이 올바르지 않습니다.")
        models = []
        for item in payload["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("실행 모델 이름이 올바르지 않습니다.")
            digest = item.get("digest", "")
            models.append({
                "name": item["name"], "digest": digest,
                "id": digest[:12] if isinstance(digest, str) else "",
                "size": item.get("size"), "size_vram": item.get("size_vram"),
                "processor": ollama_processor(item.get("size"), item.get("size_vram")),
                "context_length": item.get("context_length"), "expires_at": item.get("expires_at"),
            })
        return JSONResponse({"models": models, "checked_at": time.time()}, headers={"Cache-Control": "no-store"})
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Ollama 실행 상태를 확인하지 못했습니다: {exc}") from exc


@app.get("/api/live")
def live_console() -> StreamingResponse:
    subscriber, initial = EXPLORER.subscribe()

    def events():
        try:
            yield f"event: snapshot\ndata: {json.dumps({'text': initial}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event_type, chunk = subscriber.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event_type}\ndata: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        finally:
            EXPLORER.unsubscribe(subscriber)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/export")
def export_state() -> Response:
    body = json.dumps(EXPLORER.snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="juice-explorer-run.json"'},
    )


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}":
        raise HTTPException(status_code=403, detail="Invalid origin")
    if request.headers.get("sec-fetch-site") not in {None, "same-origin"}:
        raise HTTPException(status_code=403, detail="Cross-site request blocked")


@app.post("/api/start")
async def start(request: Request) -> dict:
    check_origin(request)
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        target = body.get("target", TARGET) if isinstance(body, dict) else TARGET
        model = body.get("model", MODEL) if isinstance(body, dict) else MODEL
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("설치된 Ollama 모델을 선택하세요.")
        models = await run_in_threadpool(get_ollama_models)
        if model not in {item["name"] for item in models}:
            raise ValueError(f"설치된 Ollama 모델을 찾지 못했습니다: {model}. 모델 목록을 새로고침하세요.")
        if isinstance(body, dict) and "model_settings" in body:
            settings = validate_model_settings(body["model_settings"])
            started = EXPLORER.start(target, model, model_settings=settings)
        else:
            started = EXPLORER.start(target, model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not started:
        raise HTTPException(status_code=409, detail="Already running")
    return {"started": True}


@app.post("/api/stop")
def stop(request: Request) -> dict:
    check_origin(request)
    EXPLORER.stop()
    return {"stopping": True}


@app.post("/api/reset")
def reset(request: Request) -> dict:
    check_origin(request)
    EXPLORER.reset()
    return {"reset": True}


@app.get("/api/lab")
def lab_status() -> JSONResponse:
    return JSONResponse(get_lab_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/lab/start")
def lab_start(request: Request) -> dict:
    check_origin(request)
    try:
        return start_lab()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")


def main() -> None:
    import uvicorn

    print(f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/")
    print(f"Target: {TARGET} | Ollama: {MODEL}")
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning")


if __name__ == "__main__":
    main()
