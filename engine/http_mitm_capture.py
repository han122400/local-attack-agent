"""Explicit opt-in MITM observations, isolated from literal HTTP evidence.

No dependency import, CA creation, or interception occurs on module import.
The optional backend runs in a separate modern-Python process. Its output is
parsed/reconstructed material and must never become http_exchange_recorded.
"""

import base64
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import uuid
from urllib.parse import urlsplit


_IPC_LIMIT = 1024 * 1024
_BODY_LIMIT = 64 * 1024
_HEADER_LIMIT = 16 * 1024


class OptionalMitmProxy:
    def __init__(self, target, on_observation, confdir, *, startup_timeout=15,
                 upstream_trusted_ca=None, python=None):
        self.on_observation = on_observation
        self.ca_file = Path(confdir).resolve() / "mitmproxy-ca-cert.pem"
        self.address = None
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._pending = {}
        self._active = None
        self._closed = False
        self._error = None
        isolated = Path(__file__).resolve().parents[1] / ".deps" / "mitm-env"
        isolated = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = python or (str(isolated) if isolated.is_file() else sys.executable)
        self._process = subprocess.Popen(
            [executable, "-u", str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self._send({"type": "start", "target": target, "confdir": str(self.ca_file.parent),
                        "upstream_trusted_ca": str(upstream_trusted_ca) if upstream_trusted_ca else None})
            if not self._ready.wait(startup_timeout):
                raise RuntimeError("Optional MITM startup timed out")
            if self._error or not self.address or not self.ca_file.is_file():
                raise RuntimeError(self._error or "Optional MITM did not become ready")
        except BaseException:
            self.close()
            raise

    def _send(self, message):
        raw = json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
        if len(raw) > _IPC_LIMIT:
            raise ValueError("MITM control message exceeds limit")
        with self._send_lock:
            self._process.stdin.write(raw)
            self._process.stdin.flush()

    def _read(self):
        try:
            while True:
                raw = self._process.stdout.readline(_IPC_LIMIT + 1)
                if not raw:
                    break
                if len(raw) > _IPC_LIMIT or not raw.endswith(b"\n"):
                    raise ValueError("Oversized optional MITM message")
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "ready":
                    self.address = message["address"]
                    self._ready.set()
                elif kind == "error":
                    self._error = message.get("error", "Optional MITM failed")
                    self._ready.set()
                elif kind == "ack":
                    with self._lock:
                        event = self._pending.get(message.get("id"))
                        if event:
                            event.set()
                elif kind == "observation":
                    with self._lock:
                        active = self._active
                        if active and active[0] == message.get("token"):
                            snapshot = message["snapshot"]
                            # Enforce the trust boundary again at the parent endpoint.
                            snapshot.update(representation="mitmproxy_reconstructed", literal=False,
                                            verification_eligible=False)
                            try:
                                self.on_observation(snapshot, active[1])
                            except Exception:
                                pass
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._error = self._error or "Optional MITM process stopped"
            self._ready.set()
            with self._lock:
                for event in self._pending.values():
                    event.set()

    def _command(self, kind, **values):
        identity, event = uuid.uuid4().hex, threading.Event()
        with self._lock:
            self._pending[identity] = event
        try:
            self._send({"type": kind, "id": identity, **values})
            if not event.wait(5) or self._process.poll() is not None:
                raise RuntimeError("Optional MITM control unavailable")
        finally:
            with self._lock:
                self._pending.pop(identity, None)

    def activate(self, binding):
        token = uuid.uuid4().hex
        with self._lock:
            if self._closed:
                raise RuntimeError("Optional MITM is closed")
            # Binding is an opaque parent-owned sink and never crosses IPC.
            self._active = (token, binding)
        try:
            self._command("activate", token=token)
        except BaseException:
            with self._lock:
                if self._active and self._active[0] == token:
                    self._active = None
            raise
        return token

    def deactivate(self, token):
        with self._lock:
            if not self._active or self._active[0] != token:
                return
        # Child acknowledgement follows already queued observations on the pipe.
        # Drain those while the old identity is still active, before exec_finished.
        try:
            self._command("deactivate", token=token)
        finally:
            with self._lock:
                if self._active and self._active[0] == token:
                    self._active = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._active = None
        try:
            if self._process.poll() is None:
                try:
                    self._send({"type": "stop"})
                    self._process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self._process.kill()
                    self._process.wait(timeout=5)
        finally:
            self._reader.join(timeout=2)
            self._process.stdin.close()
            self._process.stdout.close()


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


async def _serve(config, emit):
    import asyncio
    from mitmproxy import options
    from mitmproxy.tools.dump import DumpMaster

    target = _origin(config["target"])
    if target[0] not in ("http", "https") or not target[1]:
        raise ValueError("Optional MITM requires an HTTP(S) target")
    confdir = Path(config["confdir"])
    confdir.mkdir(parents=True, exist_ok=True)
    opts = options.Options(listen_host="127.0.0.1", listen_port=0, confdir=str(confdir),
                           allow_hosts=["^" + re.escape(f"{target[1]}:{target[2]}") + "$"])
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    if config.get("upstream_trusted_ca"):
        opts.update(ssl_verify_upstream_trusted_ca=config["upstream_trusted_ca"])
    ready = asyncio.Event()

    class Observer:
        active = None
        count = 0

        def __init__(self):
            self.bypass = set()

        def running(self):
            addresses = master.addons.get("proxyserver").listen_addrs()
            if not addresses:
                raise RuntimeError("Optional MITM listener unavailable")
            emit({"type": "ready", "address": f"http://127.0.0.1:{addresses[0][1]}"})
            ready.set()

        def tls_clienthello(self, data):
            if data.context.server.address in self.bypass:
                data.ignore_connection = True

        def tls_failed_client(self, data):
            # The failed connection cannot be salvaged; bypass protects later ones.
            self.bypass.add(data.context.server.address)

        def tls_failed_server(self, data):
            self.bypass.add(data.context.server.address)

        def requestheaders(self, flow):
            flow.request.stream = True
            if not self.active or self.count >= 32 or _origin(flow.request.url) != target:
                return
            self.count += 1
            record = {"token": self.active, "request_body": bytearray(), "response_body": bytearray(),
                      "request_oversize": False, "response_oversize": False}
            flow.metadata["v46_observation"] = record
            flow.request.stream = self.stream(record, "request")

        def responseheaders(self, flow):
            flow.response.stream = True
            record = flow.metadata.get("v46_observation")
            if record:
                flow.response.stream = self.stream(record, "response")

        @staticmethod
        def stream(record, side):
            def observe(chunk):
                if not record[side + "_oversize"]:
                    if len(record[side + "_body"]) + len(chunk) <= _BODY_LIMIT:
                        record[side + "_body"].extend(chunk)
                    else:
                        record[side + "_body"].clear()
                        record[side + "_oversize"] = True
                return chunk
            return observe

        @staticmethod
        def message(value, record, side):
            headers = value.headers.fields
            if sum(len(key) + len(item) for key, item in headers) > _HEADER_LIMIT:
                raise ValueError("Observation headers exceed limit")
            result = {"http_version": value.http_version,
                      "headers": [[key.decode("latin-1"), item.decode("latin-1")] for key, item in headers]}
            if record[side + "_oversize"]:
                result["body_omitted"] = "size_limit"
            else:
                result["body_base64"] = base64.b64encode(record[side + "_body"]).decode("ascii")
            return result

        def response(self, flow):
            record = flow.metadata.pop("v46_observation", None)
            if not record or record["token"] != self.active:
                return
            try:
                request = self.message(flow.request, record, "request")
                request.update(url=flow.request.url, method=flow.request.method)
                response = self.message(flow.response, record, "response")
                response.update(status=flow.response.status_code)
                emit({"type": "observation", "token": record["token"], "snapshot": {
                    "representation": "mitmproxy_reconstructed", "literal": False,
                    "verification_eligible": False, "request": request, "response": response}})
            except Exception:
                pass

    observer = Observer()
    master.addons.add(observer)

    async def control():
        await ready.wait()
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline, _IPC_LIMIT + 1)
            if not raw or len(raw) > _IPC_LIMIT:
                master.shutdown()
                return
            command = json.loads(raw)
            kind = command.get("type")
            if kind == "stop":
                master.shutdown()
                return
            if kind == "activate":
                observer.active, observer.count = command["token"], 0
            elif kind == "deactivate" and command.get("token") == observer.active:
                observer.active = None
            emit({"type": "ack", "id": command.get("id")})

    task = asyncio.create_task(control())
    try:
        await master.run()
    finally:
        task.cancel()


def _worker():
    import asyncio

    outgoing = queue.Queue(maxsize=40)

    def emit(message):
        # Observation backpressure only loses samples; it never stalls forwarding.
        if message.get("type") == "observation":
            if outgoing.qsize() >= 8:
                return
            try:
                outgoing.put_nowait(message)
            except queue.Full:
                pass
        else:
            outgoing.put(message, timeout=5)

    def writer():
        while True:
            message = outgoing.get()
            try:
                raw = json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n"
                if len(raw) <= _IPC_LIMIT:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
            except OSError:
                return
            finally:
                outgoing.task_done()

    threading.Thread(target=writer, daemon=True).start()
    try:
        raw = sys.stdin.buffer.readline(_IPC_LIMIT + 1)
        if len(raw) > _IPC_LIMIT:
            raise ValueError("MITM initialization exceeds limit")
        asyncio.run(_serve(json.loads(raw), emit))
    except BaseException as exc:
        emit({"type": "error", "error": f"Optional MITM unavailable: {type(exc).__name__}: {exc}"[:1000]})
    outgoing.join()


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    _worker()
