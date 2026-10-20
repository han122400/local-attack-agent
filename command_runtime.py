"""Per-run local command and persistent Playwright execution.

The runtime folder is a workspace boundary for file APIs, not an OS sandbox
for arbitrary CMD or Python code.
"""

from __future__ import annotations

import hashlib
import codecs
import json
import os
import signal
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from code_checks import check_generated_code
from session_log import LogFailure, SessionLog, safe_relative_path, validate_run_id


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _new_process_job(process: subprocess.Popen):
    """Hold a Windows kill-on-close job for the process and its descendants."""
    if os.name != "nt":
        return None
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    try:
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        if not _kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        return job
    except Exception:
        _kernel32.CloseHandle(job)
        raise


def installed_chromium() -> str | None:
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if configured and Path(configured).is_file():
        return configured
    browser_root = Path.home() / "AppData" / "Local" / "ms-playwright"
    matches = sorted(browser_root.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"))
    return str(matches[-1]) if matches else None


WORKER_SOURCE = r'''import ast
import asyncio
import contextlib
import inspect
import json
import os
import socket
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

project = Path(sys.argv[5])
sys.path.insert(0, str(project / ".deps"))
from playwright.async_api import async_playwright

def send(sock, item):
    sock.sendall((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))

def recv(sock, buffer):
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError("controller disconnected")
        buffer += chunk
    line, buffer = buffer.split(b"\n", 1)
    return json.loads(line), buffer

async def main():
    host, port, token, target, project_root, executable = sys.argv[1:7]
    sock = socket.create_connection((host, int(port)), timeout=20)
    sock.settimeout(None)
    send(sock, {"type": "hello", "token": token})
    target_parts = urlsplit(target)
    async with async_playwright() as playwright:
        options = {"headless": True}
        if executable:
            options["executable_path"] = executable
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(service_workers="block", accept_downloads=False)
        async def guard(route):
            request = route.request
            parts = urlsplit(request.url)
            allowed = (parts.scheme == target_parts.scheme and parts.hostname == target_parts.hostname
                       and parts.port == target_parts.port and request.method in ("GET", "HEAD")
                       and parts.username is None and parts.password is None)
            if allowed:
                await route.continue_()
            else:
                await route.abort()
        await context.route("**/*", guard)
        page = await context.new_page()
        target_origin = target_parts.scheme + "://" + target_parts.netloc
        def _same_origin(url):
            p = urlsplit(url)
            return (p.scheme == target_parts.scheme and p.hostname == target_parts.hostname
                    and p.port == target_parts.port)
        async def get_text(url):
            """GET a URL within the target origin and return its response text."""
            if not _same_origin(url):
                raise ValueError("get_text is restricted to the target origin " + target_origin)
            response = await context.request.get(url)
            return await response.text()
        async def get_json(url):
            """GET a URL within the target origin and return parsed JSON."""
            if not _same_origin(url):
                raise ValueError("get_json is restricted to the target origin " + target_origin)
            response = await context.request.get(url)
            return await response.json()
        async def goto_and_render(url, timeout=15000, settle_ms=1200):
            """Navigate within the target origin and let the SPA route render.

            Uses domcontentloaded + a short settle wait. networkidle is avoided on purpose:
            this app keeps polling, so networkidle may never fire and would hang until timeout.
            """
            if not _same_origin(url):
                raise ValueError("goto_and_render is restricted to the target origin " + target_origin)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(settle_ms)
            return page
        async def http_get(url):
            """GET within the target origin and return a structured observation that never raises on
            non-JSON: status, ok, content type, length, a body snippet, and parsed json or a parse_error."""
            if not _same_origin(url):
                raise ValueError("http_get is restricted to the target origin " + target_origin)
            response = await context.request.get(url)
            body = await response.text()
            ctype = response.headers.get("content-type", "")
            result = {"url": url, "status": response.status, "ok": response.ok,
                      "content_type": ctype, "length": len(body), "body": body[:2000]}
            if "json" in ctype:
                try:
                    result["json"] = json.loads(body)
                except Exception as exc:
                    result["parse_error"] = str(exc)[:200]
            return result
        async def list_inputs():
            """Return the page's visible inputs (index/tag/type/name/id/label/value) and nearby button
            labels, so you can pick the right field instead of guessing with a raw selector."""
            return await page.evaluate("""() => {
              const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
              const label = el => (el.getAttribute('aria-label') || el.getAttribute('placeholder')
                || (el.labels && el.labels[0] ? el.labels[0].textContent : '') || el.getAttribute('name') || '').trim().slice(0, 80);
              const inputs = [...document.querySelectorAll('input,textarea,select')].filter(vis).slice(0, 30).map((el, i) => ({
                index: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '', id: el.id || '', label: label(el), value: (el.value || '').slice(0, 40) }));
              const buttons = [...document.querySelectorAll('button,[role=button],input[type=submit]')].filter(vis).slice(0, 20)
                .map(el => (el.getAttribute('aria-label') || el.textContent || el.value || '').trim().slice(0, 40)).filter(Boolean);
              return { inputs, buttons };
            }""")
        async def submit_input(index, value, result_selector=None, settle_ms=1200):
            """Fill the visible input at `index` (from list_inputs), submit with Enter, wait, and return a
            structured result: url before/after, title, and — if result_selector is given — the count and a
            sample of matching elements. Use the SAME result_selector for the normal and the test input to
            compare counts/content. Avoids the fragile fill/select/submit code written by hand."""
            filled = await page.evaluate("""([i, val]) => {
              const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
              const el = [...document.querySelectorAll('input,textarea,select')].filter(vis)[i];
              if (!el) return false;
              el.focus(); el.value = val;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }""", [index, value])
            if not filled:
                return {"index": index, "value": value, "submitted": False, "error": "no visible input at index"}
            url_before = page.url
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(settle_ms)
            result = {"index": index, "value": value, "submitted": True, "url_before": url_before,
                      "url_after": page.url, "url_changed": page.url != url_before, "title": await page.title()}
            if result_selector:
                found = await page.evaluate("""sel => {
                  const els = [...document.querySelectorAll(sel)];
                  return { count: els.length, sample: els.slice(0, 5).map(e => (e.textContent || '').trim().slice(0, 80)) };
                }""", result_selector)
                result["result_count"] = found["count"]
                result["result_sample"] = found["sample"]
            else:
                result["text_sample"] = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 400)")
            return result
        helpers = {"get_text": get_text, "get_json": get_json, "http_get": http_get,
                   "goto_and_render": goto_and_render, "list_inputs": list_inputs,
                   "submit_input": submit_input, "target_origin": target_origin}
        send(sock, {"type": "ready"})
        buffer = b""
        try:
            while True:
                message, buffer = await asyncio.to_thread(recv, sock, buffer)
                if message.get("type") == "stop":
                    break
                if message.get("type") != "execute":
                    continue
                exec_id = message["exec_id"]
                source = Path(message["path"])
                limit = message.get("timeout")
                timed_out = False
                failure_source, cause_code, exception_type = None, None, None
                with open(message["stdout_path"], "w", encoding="utf-8", buffering=1) as out, \
                     open(message["stderr_path"], "w", encoding="utf-8", buffering=1) as err:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        async def run_user_code():
                            namespace = {"__name__": "__agent__", "__file__": str(source),
                                         "browser": browser, "context": context, "page": page,
                                         "asyncio": asyncio, **helpers}
                            tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
                            # If the code already calls main() at top level, do not auto-call it again;
                            # otherwise the user's main ran twice and its output was duplicated.
                            def _is_main_call(stmt):
                                v = stmt.value.value if isinstance(getattr(stmt, "value", None), ast.Await) else getattr(stmt, "value", None)
                                return isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "main"
                            called_main = any(isinstance(s, ast.Expr) and _is_main_call(s) for s in tree.body)
                            compiled = compile(tree, str(source), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                            result = eval(compiled, namespace, namespace)
                            if inspect.isawaitable(result):
                                await result
                            entry = namespace.get("main")
                            if callable(entry) and not called_main:
                                result = entry(page, context, browser)
                                if inspect.isawaitable(result):
                                    await result
                        try:
                            # Cancel the user coroutine on timeout WITHOUT killing this worker,
                            # so the browser survives for the next command.
                            if isinstance(limit, (int, float)) and limit > 0:
                                await asyncio.wait_for(run_user_code(), limit)
                            else:
                                await run_user_code()
                            exit_code, detail = 0, ""
                        except asyncio.TimeoutError:
                            timed_out = True
                            failure_source, cause_code = "execution", "execution_timeout"
                            exit_code = None
                            detail = "Browser code timed out"
                            print(detail, file=sys.stderr, flush=True)
                        except BaseException as exc:
                            exception_type = type(exc).__name__
                            failure_source = "generated_code" if browser.is_connected() else "system"
                            cause_code = "python_exception" if browser.is_connected() else "browser_disconnected"
                            detail = traceback.format_exc()
                            print(detail, file=sys.stderr, flush=True)
                            exit_code = 1
                        out.flush()
                        err.flush()
                send(sock, {"type": "result", "exec_id": exec_id, "exit_code": exit_code,
                            "timed_out": timed_out, "exception": detail[-4000:],
                            "failure_source": failure_source, "cause_code": cause_code,
                            "exception_type": exception_type})
        finally:
            await context.close()
            await browser.close()
            sock.close()

if __name__ == "__main__":
    asyncio.run(main())
'''


@dataclass
class ExecutionResult:
    exec_id: str
    action: str
    path: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    exception: str = ""
    elapsed: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    artifact_path: str = ""
    failure_source: str | None = None
    cause_code: str | None = None
    exception_type: str | None = None
    retryable: bool | None = None


class CommandRuntime:
    def __init__(self, runs_root: Path, log: SessionLog, *, python: str | None = None):
        self.log = log
        self.run_id = log.run_id
        self.runtime_dir = Path(runs_root).resolve() / self.run_id / "runtime"
        self.python = python or sys.executable
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._owned: dict[int, dict] = {}
        self._jobs: dict[int, object] = {}
        self._worker: subprocess.Popen | None = None
        self._conn: socket.socket | None = None
        self._control_buffer = b""
        self._active_exec_id: str | None = None
        self._call_by_exec: dict[str, str] = {}
        self._output: dict[str, dict] = {}
        self._output_error: LogFailure | None = None
        self._output_threads: list[threading.Thread] = []
        self._exec_counter = 0
        self._browser_enabled = False
        self.target = "http://127.0.0.1:3000/"

    def _check_run(self, run_id: str) -> None:
        if validate_run_id(run_id) != self.run_id:
            raise ValueError("Runtime belongs to another run")

    def _environment(self) -> dict:
        env = dict(os.environ)
        temporary = self.runtime_dir / "tmp"
        env.update({"TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
                    "PIP_CACHE_DIR": str(temporary / "pip-cache"),
                    "PYTHONPYCACHEPREFIX": str(temporary / "pycache"),
                    "npm_config_cache": str(temporary / "npm-cache")})
        return env

    def start_session(self, run_id: str, *, target: str = "http://127.0.0.1:3000/", browser: bool = True) -> None:
        self._check_run(run_id)
        with self._lock:
            if self.runtime_dir.exists():
                raise RuntimeError("Runtime already exists")
            self.target = target
            self.log.update(runtime_path=str(self.runtime_dir), runtime_creation_intent=True)
            for folder in ("tools", "work", "tmp", "deps", "browser"):
                (self.runtime_dir / folder).mkdir(parents=True, exist_ok=True)
            self.log.append("runtime_started", {"runtime_path": str(self.runtime_dir)})
            self._browser_enabled = browser
            if browser:
                self._start_browser_worker()

    def _register(self, process: subprocess.Popen, role: str, exec_id: str = "") -> None:
        try:
            job = _new_process_job(process)
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise
        try:
            created = psutil.Process(process.pid).create_time()
        except Exception:
            if job is not None:
                self._jobs[process.pid] = job
                self._close_job(process.pid, terminate=True)
            process.wait(timeout=5)
            raise
        record = {"pid": process.pid, "created_at": created, "role": role, "exec_id": exec_id,
                  "run_id": self.run_id}
        self._owned[process.pid] = record
        if job is not None:
            self._jobs[process.pid] = job
        try:
            self.log.update(managed_processes=list(self._owned.values()))
            self.log.append("process_started", {"exec_id": exec_id, "process": record})
        except Exception:
            self._kill_tree(process, record)
            self._owned.pop(process.pid, None)
            self._close_job(process.pid)
            raise

    def _unregister(self, process: subprocess.Popen) -> None:
        if process.pid in self._jobs:
            # A descendant can outlive CMD. Keep the job and ownership record
            # until stop() terminates the whole group.
            return
        record = self._owned.pop(process.pid, None)
        self.log.update(managed_processes=list(self._owned.values()))
        if record:
            self.log.append("process_stopped", {"exec_id": record["exec_id"], "process": record})

    def _close_job(self, pid: int, *, terminate: bool = False) -> None:
        job = self._jobs.pop(pid, None)
        if job is None:
            return
        if terminate:
            if not _kernel32.TerminateJobObject(job, 1):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                raise OSError(error, "TerminateJobObject failed")
        _kernel32.CloseHandle(job)

    def _kill_tree(self, process: subprocess.Popen, record: dict) -> None:
        if process.pid in self._jobs:
            self._close_job(process.pid, terminate=True)
        try:
            current = psutil.Process(record["pid"])
            if abs(current.create_time() - record["created_at"]) > 0.01:
                raise RuntimeError("PID creation time changed; refusing to kill unrelated process")
            descendants = current.children(recursive=True)
            for child in reversed(descendants):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            current.terminate()
            _, alive = psutil.wait_procs(descendants + [current], timeout=2)
            for item in alive:
                item.kill()
            psutil.wait_procs(alive, timeout=2)
        except psutil.NoSuchProcess:
            pass
        process.wait(timeout=5)

    def _start_browser_worker(self) -> None:
        worker_path = self.runtime_dir / "tools" / "_browser_worker.py"
        worker_path.write_text(WORKER_SOURCE, encoding="utf-8")
        token = uuid.uuid4().hex
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(20)
        executable = installed_chromium() or ""
        args = [self.python, "-u", str(worker_path), "127.0.0.1", str(server.getsockname()[1]),
                token, self.target, str(Path(__file__).resolve().parent), executable]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(args, cwd=self.runtime_dir, env=self._environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=flags, bufsize=0)
        self._worker = process
        self._register(process, "browser_worker")
        self._start_output_readers(process, "")
        try:
            conn, _ = server.accept()
            self._conn = conn
            hello = self._wait_control(5)
            if hello.get("type") != "hello" or hello.get("token") != token:
                raise RuntimeError("Browser worker handshake failed")
            ready = self._wait_control(30)
            if ready.get("type") != "ready":
                raise RuntimeError("Browser worker did not become ready")
            self.log.append("browser_ready", {"pid": process.pid, "target": self.target})
        except Exception:
            self._kill_tree(process, self._owned[process.pid])
            self._unregister(process)
            self._worker = None
            raise
        finally:
            server.close()

    def _send_control(self, message: dict) -> None:
        if self._conn is None:
            raise RuntimeError("Browser worker is unavailable")
        self._conn.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def _wait_control(self, timeout: float, on_poll=None) -> dict:
        if self._conn is None:
            raise RuntimeError("Browser worker is unavailable")
        deadline = time.monotonic() + timeout
        while True:
            if on_poll is not None:
                on_poll()
            if b"\n" in self._control_buffer:
                line, self._control_buffer = self._control_buffer.split(b"\n", 1)
                return json.loads(line)
            if self._cancel.is_set():
                raise InterruptedError("Execution cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Browser code timed out")
            readable, _, _ = select.select([self._conn], [], [], min(0.1, remaining))
            if readable:
                chunk = self._conn.recv(65536)
                if not chunk:
                    raise RuntimeError("Browser worker exited")
                self._control_buffer += chunk

    def _add_output(self, exec_id: str, channel: str, chunk: str) -> None:
        with self._lock:
            if not chunk:
                return
            try:
                kind = "exec_output" if exec_id else "worker_output"
                self.log.append(kind, {"exec_id": exec_id, "call_id": self._call_by_exec.get(exec_id, ""),
                                       "channel": channel, "text": chunk})
            except LogFailure as exc:
                self._output_error = exc
                self._cancel.set()
                return
            record = self._output.setdefault(exec_id, {"stdout": "", "stderr": "", "truncated": False})
            prior = record[channel]
            if len(prior) < 65536:
                record[channel] = (prior + chunk)[:65536]
            if len(prior) + len(chunk) > 65536:
                record["truncated"] = True

    def _start_output_readers(self, process: subprocess.Popen, exec_id: str) -> list[threading.Thread]:
        threads = []
        for channel in ("stdout", "stderr"):
            pipe = getattr(process, channel)
            def read_output(stream=pipe, name=channel):
                try:
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        self._add_output(exec_id, name, chunk.decode("utf-8", errors="replace"))
                finally:
                    stream.close()
            thread = threading.Thread(target=read_output, daemon=True)
            thread.start()
            threads.append(thread)
            self._output_threads.append(thread)
        return threads

    def _file_path(self, relative: str, *, writing: bool) -> Path:
        clean = safe_relative_path(relative)
        target = self.runtime_dir / clean
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(self.runtime_dir) or target.is_symlink():
            raise ValueError("File path escapes runtime")
        if writing:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.parent.resolve().is_relative_to(self.runtime_dir):
                raise ValueError("File path escapes runtime")
        return target

    def _tail_python_output(self, paths: dict, offsets: dict, decoders: dict,
                            exec_id: str, *, final: bool = False) -> None:
        for channel, path in paths.items():
            with path.open("rb") as stream:
                stream.seek(offsets[channel])
                while True:
                    data = stream.read(65536)
                    if not data:
                        break
                    offsets[channel] += len(data)
                    chunk = decoders[channel].decode(data)
                    if chunk:
                        self._add_output(exec_id, channel, chunk)
            if final:
                remaining = decoders[channel].decode(b"", final=True)
                if remaining:
                    self._add_output(exec_id, channel, remaining)

    def _run_command(self, command: str, exec_id: str, timeout: float) -> int:
        shell = ["cmd.exe", "/D", "/S", "/C", command] if os.name == "nt" else ["/bin/sh", "-c", command]
        process = subprocess.Popen(shell, cwd=self.runtime_dir, env=self._environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), bufsize=0)
        self._register(process, "command", exec_id)
        owner = self._owned[process.pid]
        threads = self._start_output_readers(process, exec_id)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if self._cancel.is_set():
                    self._kill_tree(process, owner)
                    raise InterruptedError("Execution cancelled")
                if time.monotonic() >= deadline:
                    self._kill_tree(process, owner)
                    raise TimeoutError("Command timed out")
                time.sleep(0.05)
            for thread in threads:
                thread.join(timeout=2)
            return process.returncode
        finally:
            if process.poll() is None:
                self._kill_tree(process, owner)
            for thread in threads:
                thread.join(timeout=2)
            self._unregister(process)

    def run(self, run_id: str, command: dict, *, call_id: str = "", timeout: float = 30,
            on_output=None) -> ExecutionResult:
        self._check_run(run_id)
        if not self.runtime_dir.is_dir():
            raise RuntimeError("Runtime is absent")
        if self._cancel.is_set():
            raise InterruptedError("Runtime is stopping")
        self._exec_counter += 1
        exec_id = f"EXEC-{self._exec_counter:04d}"
        action = command.get("action", "") if isinstance(command, dict) else ""
        result = ExecutionResult(exec_id=exec_id, action=action)
        stage = "input"
        began = time.monotonic()
        self._call_by_exec[exec_id] = call_id
        self.log.append("exec_started", {"exec_id": exec_id, "call_id": call_id, "action": action,
                                         "cwd": str(self.runtime_dir)})
        try:
            if action == "read_file":
                stage = "file_api"
                path = self._file_path(command["path"], writing=False)
                result.path = str(path)
                result.stdout = path.read_text(encoding="utf-8")[:65536]
                result.exit_code = 0
            elif action == "write_file":
                stage = "file_api"
                path = self._file_path(command["path"], writing=True)
                result.path = str(path)
                content = command["content"]
                if not isinstance(content, str) or len(content.encode("utf-8")) > 2_000_000:
                    raise ValueError("Invalid file content")
                path.write_text(content, encoding="utf-8")
                self.log.append("file_changed", {"exec_id": exec_id, "call_id": call_id,
                                                  "path": command["path"], "change": "write", "bytes": path.stat().st_size})
                result.exit_code = 0
            elif action == "run_command":
                script = command["command"]
                if not isinstance(script, str) or not script.strip():
                    raise ValueError("Command text required")
                stage = "executor"
                snapshot = self.log.preserve_bytes(f"commands/{exec_id}.cmd", script.encode("utf-8"))
                result.artifact_path = snapshot["path"]
                result.path = str(self.runtime_dir)
                result.exit_code = self._run_command(script, exec_id, timeout)
                if result.exit_code != 0:
                    result.failure_source, result.cause_code = "command", "command_exit_nonzero"
                    result.retryable = True
            elif action == "run_python":
                stage = "executor"
                if not self._browser_enabled:
                    raise RuntimeError("Browser worker is unavailable")
                if self._worker is None or self._worker.poll() is not None:
                    # A prior worker was hard-killed (unresponsive timeout) or crashed. Bring a
                    # fresh browser back so the session keeps working; browser state resets.
                    if self._worker is not None:
                        self._unregister(self._worker)
                        self._worker = None
                    if self._conn is not None:
                        try:
                            self._conn.close()
                        except OSError:
                            pass
                        self._conn = None
                    self._control_buffer = b""
                    self.log.append("browser_respawn", {"exec_id": exec_id, "reason": "worker_unavailable"})
                    self._start_browser_worker()
                stage = "input"
                code = command["code"]
                if not isinstance(code, str) or not code.strip():
                    raise ValueError("Python code required")
                stage = "executor"
                source = self.runtime_dir / "tools" / f"{exec_id}.py"
                source.write_text(code, encoding="utf-8")
                snapshot = self.log.preserve_bytes(f"code/{exec_id}.py", code.encode("utf-8"))
                result.artifact_path = snapshot["path"]
                result.path = str(source)
                problems = check_generated_code(code)
                if problems:
                    # Catch the recurring mistakes before running: feed a precise error back instead of a
                    # raw traceback, and skip the browser round-trip entirely.
                    stage = "input"
                    result.exit_code = 1
                    result.failure_source, result.cause_code, result.retryable = "generated_code", "static_check", True
                    result.exception = "Static check failed before running:\n- " + "\n- ".join(problems)
                    self._add_output(exec_id, "stderr", result.exception + "\n")
                else:
                    paths = {name: self.runtime_dir / "tmp" / f"{exec_id}.{name}" for name in ("stdout", "stderr")}
                    for path in paths.values():
                        path.touch()
                    offsets = {name: 0 for name in paths}
                    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in paths}
                    def poll_output():
                        self._tail_python_output(paths, offsets, decoders, exec_id)
                    self._send_control({"type": "execute", "exec_id": exec_id, "path": str(source),
                                        "stdout_path": str(paths["stdout"]), "stderr_path": str(paths["stderr"]),
                                        "timeout": timeout})
                    # The worker cancels the code at `timeout` and reports back; give it extra grace
                    # so we only hard-kill (and lose the browser) when the worker is truly unresponsive.
                    reply = self._wait_control(timeout + 10, on_poll=poll_output)
                    self._tail_python_output(paths, offsets, decoders, exec_id, final=True)
                    if reply.get("type") != "result" or reply.get("exec_id") != exec_id:
                        raise RuntimeError("Browser worker response mismatch")
                    result.exit_code = reply.get("exit_code")
                    result.exception = reply.get("exception", "")
                    result.failure_source = reply.get("failure_source")
                    result.cause_code = reply.get("cause_code")
                    result.exception_type = reply.get("exception_type")
                    if result.exit_code != 0:
                        result.retryable = True
                    if reply.get("timed_out"):
                        result.timed_out = True
                        result.exit_code = None
                    time.sleep(0.05)
            elif action == "finish":
                result.exit_code = 0
            else:
                raise ValueError("Unknown runtime action")
        except TimeoutError as exc:
            result.timed_out = True
            result.exception = str(exc)
            result.exit_code = None
            result.failure_source = "system" if action == "run_python" else "execution"
            result.cause_code = "browser_worker_unresponsive" if action == "run_python" else "execution_timeout"
            result.retryable = True
            if action == "run_python" and self._worker is not None:
                self._kill_tree(self._worker, self._owned[self._worker.pid])
                self._unregister(self._worker)
                self._worker = None
        except InterruptedError as exc:
            result.cancelled = True
            result.exception = str(exc)
            result.exit_code = None
            result.failure_source, result.cause_code, result.retryable = "control", "execution_cancelled", False
        except LogFailure:
            self._cancel.set()
            raise
        except Exception as exc:
            result.exception = str(exc)
            result.exit_code = 1
            result.exception_type = type(exc).__name__
            input_failure = stage == "input" or (stage == "file_api" and isinstance(
                exc, (ValueError, KeyError, FileNotFoundError, PermissionError, UnicodeError)))
            result.failure_source = "input" if input_failure else "system"
            result.cause_code = "invalid_execution_input" if input_failure else "executor_failure"
            result.retryable = True
        finally:
            self._active_exec_id = None
            output = self._output.pop(exec_id, {})
            result.stdout = result.stdout or output.get("stdout", "")
            result.stderr = output.get("stderr", "")
            result.output_truncated = output.get("truncated", False)
            result.elapsed = round(time.monotonic() - began, 3)
            if self._output_error is not None:
                raise self._output_error
            self.log.append("exec_finished", {"exec_id": exec_id, "call_id": call_id, **asdict(result)})
            self._call_by_exec.pop(exec_id, None)
        return result

    def stop(self, run_id: str, timeout: float = 10, *, record: bool = True) -> None:
        self._check_run(run_id)
        self._cancel.set()
        worker = self._worker
        if self._conn is not None:
            try:
                self._send_control({"type": "stop"})
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        errors = []
        for pid, record in list(self._owned.items()):
            try:
                if pid in self._jobs:
                    self._close_job(pid, terminate=True)
                current = psutil.Process(pid)
                if abs(current.create_time() - record["created_at"]) > 0.01:
                    raise RuntimeError("Owned process PID was reused")
                descendants = current.children(recursive=True)
                for item in reversed(descendants):
                    item.terminate()
                current.terminate()
                _, alive = psutil.wait_procs(descendants + [current], timeout=max(0.1, min(2, deadline - time.monotonic())))
                for item in alive:
                    item.kill()
                if psutil.wait_procs(alive, timeout=max(0.1, deadline - time.monotonic()))[1]:
                    raise RuntimeError("Owned process did not stop")
            except psutil.NoSuchProcess:
                pass
            except Exception as exc:
                errors.append(exc)
                continue
            self._owned.pop(pid, None)
            if record:
                try:
                    self.log.append("process_stopped", {"exec_id": record["exec_id"], "process": record})
                except Exception as exc:
                    errors.append(exc)
        if record:
            try:
                self.log.update(managed_processes=list(self._owned.values()))
            except Exception as exc:
                errors.append(exc)
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if worker is not None:
            try:
                worker.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                errors.append(exc)
        for thread in self._output_threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        self._worker = None
        if record:
            try:
                self.log.append("runtime_stopped", {})
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Runtime stop incomplete: " + "; ".join(str(item) for item in errors))

    def cancel(self) -> None:
        """Signal in-flight work; finalization performs the owned-process stop."""
        self._cancel.set()

    def restore_owned(self, run_id: str, records: list[dict]) -> None:
        """Load only the process identities recorded by this run for crash recovery."""
        self._check_run(run_id)
        for record in records:
            if (not isinstance(record, dict) or record.get("run_id") != run_id
                    or type(record.get("pid")) is not int or type(record.get("created_at")) not in (int, float)):
                raise RuntimeError("Invalid owned process identity")
        self._owned = {record["pid"]: dict(record) for record in records}

    def export_evidence(self, run_id: str) -> list[dict]:
        self._check_run(run_id)
        evidence = []
        if not self.runtime_dir.exists():
            return evidence
        for subtree in ("tools", "work"):
            root = self.runtime_dir / subtree
            for file in root.rglob("*"):
                if file.is_symlink():
                    raise LogFailure("Linked evidence cannot be exported")
                if not file.is_file():
                    continue
                if file.stat().st_size > 10_000_000:
                    raise LogFailure(f"Evidence file too large: {file.name}")
                relative = file.relative_to(self.runtime_dir).as_posix()
                item = self.log.preserve_file(f"final/{relative}", file)
                evidence.append({"name": relative, "artifact": item})
        self.log.append("evidence_exported", {"files": evidence})
        self.log.flush()
        return evidence

    def destroy(self, run_id: str, timeout: float = 10) -> None:
        self._check_run(run_id)
        if self._owned:
            raise RuntimeError("Managed processes remain")
        expected = self.log.run_dir / "runtime"
        if self.runtime_dir != expected or self.runtime_dir.resolve() != expected.absolute():
            raise RuntimeError("Runtime path does not match owned session")
        deadline = time.monotonic() + timeout
        while self.runtime_dir.exists():
            try:
                shutil.rmtree(self.runtime_dir)
            except OSError as exc:
                # Windows can retain a Chromium profile handle briefly after
                # the job has terminated. A bounded retry preserves the
                # cleanup gate if the handle is not released.
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Runtime deletion failed: {exc}") from exc
                time.sleep(0.2)
        if self.runtime_dir.exists():
            raise RuntimeError("Runtime folder remains")
        self.log.append("runtime_destroyed", {"runtime_path": str(self.runtime_dir)})

    def inspect(self, run_id: str) -> dict:
        self._check_run(run_id)
        return {"runtime_exists": self.runtime_dir.exists(),
                "managed_processes": list(self._owned.values()),
                "browser_alive": self._worker is not None and self._worker.poll() is None}
