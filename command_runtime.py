"""Per-run local command and persistent Playwright execution.

The runtime folder is a workspace boundary for file APIs, not an OS sandbox
for arbitrary CMD or Python code.
"""

from __future__ import annotations

import hashlib
import json
import os
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

from session_log import LogFailure, SessionLog, safe_relative_path, validate_run_id


WORKER_SOURCE = r'''import ast
import asyncio
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
                try:
                    namespace = {"__name__": "__agent__", "__file__": str(source),
                                 "browser": browser, "context": context, "page": page, "asyncio": asyncio}
                    compiled = compile(source.read_text(encoding="utf-8"), str(source), "exec",
                                       flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    result = eval(compiled, namespace, namespace)
                    if inspect.isawaitable(result):
                        await result
                    entry = namespace.get("main")
                    if callable(entry):
                        result = entry(page, context, browser)
                        if inspect.isawaitable(result):
                            await result
                    sys.stdout.flush()
                    sys.stderr.flush()
                    send(sock, {"type": "result", "exec_id": exec_id, "exit_code": 0, "exception": ""})
                except BaseException:
                    detail = traceback.format_exc()
                    print(detail, file=sys.stderr, flush=True)
                    send(sock, {"type": "result", "exec_id": exec_id, "exit_code": 1,
                                "exception": detail[-4000:]})
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


class CommandRuntime:
    def __init__(self, runs_root: Path, log: SessionLog, *, python: str | None = None):
        self.log = log
        self.run_id = log.run_id
        self.runtime_dir = Path(runs_root).resolve() / self.run_id / "runtime"
        self.python = python or sys.executable
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._owned: dict[int, dict] = {}
        self._worker: subprocess.Popen | None = None
        self._conn: socket.socket | None = None
        self._control_buffer = b""
        self._active_exec_id: str | None = None
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
        created = psutil.Process(process.pid).create_time()
        record = {"pid": process.pid, "created_at": created, "role": role, "exec_id": exec_id,
                  "run_id": self.run_id}
        self._owned[process.pid] = record
        try:
            self.log.update(managed_processes=list(self._owned.values()))
            self.log.append("process_started", {"exec_id": exec_id, "process": record})
        except Exception:
            self._kill_tree(process, record)
            self._owned.pop(process.pid, None)
            raise

    def _unregister(self, process: subprocess.Popen) -> None:
        record = self._owned.pop(process.pid, None)
        self.log.update(managed_processes=list(self._owned.values()))
        if record:
            self.log.append("process_stopped", {"exec_id": record["exec_id"], "process": record})

    @staticmethod
    def _kill_tree(process: subprocess.Popen, record: dict) -> None:
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
        from explorer import installed_chromium
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

    def _wait_control(self, timeout: float) -> dict:
        if self._conn is None:
            raise RuntimeError("Browser worker is unavailable")
        deadline = time.monotonic() + timeout
        while True:
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
                self.log.append("exec_output", {"exec_id": exec_id, "channel": channel, "text": chunk})
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
                        active = exec_id or self._active_exec_id or ""
                        self._add_output(active, name, chunk.decode("utf-8", errors="replace"))
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
        began = time.monotonic()
        self.log.append("exec_started", {"exec_id": exec_id, "call_id": call_id, "action": action,
                                         "cwd": str(self.runtime_dir)})
        try:
            if action == "read_file":
                path = self._file_path(command["path"], writing=False)
                result.path = str(path)
                result.stdout = path.read_text(encoding="utf-8")[:65536]
                result.exit_code = 0
            elif action == "write_file":
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
                snapshot = self.log.preserve_bytes(f"commands/{exec_id}.cmd", script.encode("utf-8"))
                result.artifact_path = snapshot["path"]
                result.path = str(self.runtime_dir)
                result.exit_code = self._run_command(script, exec_id, timeout)
            elif action == "run_python":
                if not self._browser_enabled or self._worker is None or self._worker.poll() is not None:
                    raise RuntimeError("Browser worker is unavailable")
                code = command["code"]
                if not isinstance(code, str) or not code.strip():
                    raise ValueError("Python code required")
                source = self.runtime_dir / "tools" / f"{exec_id}.py"
                source.write_text(code, encoding="utf-8")
                snapshot = self.log.preserve_bytes(f"code/{exec_id}.py", code.encode("utf-8"))
                result.artifact_path = snapshot["path"]
                result.path = str(source)
                self._active_exec_id = exec_id
                self._send_control({"type": "execute", "exec_id": exec_id, "path": str(source)})
                reply = self._wait_control(timeout)
                if reply.get("type") != "result" or reply.get("exec_id") != exec_id:
                    raise RuntimeError("Browser worker response mismatch")
                result.exit_code = reply.get("exit_code")
                result.exception = reply.get("exception", "")
                time.sleep(0.05)
            elif action == "finish":
                result.exit_code = 0
            else:
                raise ValueError("Unknown runtime action")
        except TimeoutError as exc:
            result.timed_out = True
            result.exception = str(exc)
            result.exit_code = None
            if action == "run_python" and self._worker is not None:
                self._kill_tree(self._worker, self._owned[self._worker.pid])
                self._unregister(self._worker)
                self._worker = None
        except InterruptedError as exc:
            result.cancelled = True
            result.exception = str(exc)
            result.exit_code = None
        except LogFailure:
            self._cancel.set()
            raise
        except Exception as exc:
            result.exception = str(exc)
            result.exit_code = 1
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
        return result

    def stop(self, run_id: str, timeout: float = 10) -> None:
        self._check_run(run_id)
        self._cancel.set()
        worker = self._worker
        if self._conn is not None:
            try:
                self._send_control({"type": "stop"})
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        for pid, record in list(self._owned.items()):
            process = self._worker if self._worker and self._worker.pid == pid else None
            try:
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
            self._owned.pop(pid, None)
            self.log.append("process_stopped", {"exec_id": record["exec_id"], "process": record})
        self.log.update(managed_processes=[])
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if worker is not None:
            worker.wait(timeout=max(0.1, deadline - time.monotonic()))
        for thread in self._output_threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        self._worker = None
        self.log.append("runtime_stopped", {})

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
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)
        if self.runtime_dir.exists():
            raise RuntimeError("Runtime folder remains")
        self.log.append("runtime_destroyed", {"runtime_path": str(self.runtime_dir)})

    def inspect(self, run_id: str) -> dict:
        self._check_run(run_id)
        return {"runtime_exists": self.runtime_dir.exists(),
                "managed_processes": list(self._owned.values()),
                "browser_alive": self._worker is not None and self._worker.poll() is None}
