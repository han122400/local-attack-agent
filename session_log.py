"""Durable, run-bound session events and evidence.

This is the only persistent event sink used by the harness and runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
import stat
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from event_classification import classify_event, event_view


RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class LogFailure(RuntimeError):
    """A durable session write did not complete."""


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Invalid run_id")
    return run_id


def safe_relative_path(value: str) -> Path:
    """Validate an untrusted relative name before reading or storing evidence."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative path")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError("Absolute paths are not allowed")
    parts = value.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError("Path escapes session")
    return Path(*parts)


@contextmanager
def open_beneath(root: Path, relative: str | None, flags: int = os.O_RDONLY, *, parents=False):
    """Anchor each lookup to a directory handle; generated symlink swaps must not redirect host I/O."""
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise RuntimeError('Secure runtime file access requires a POSIX controller (macOS/Linux)')
    parts = safe_relative_path(relative).parts if relative is not None else ()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        for name in parts[:-1]:
            if parents:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        if parts:
            descriptor = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
            kind = os.fstat(descriptor).st_mode
            if not (stat.S_ISREG(kind) or (flags & os.O_DIRECTORY and stat.S_ISDIR(kind))):
                raise ValueError('Only regular files or directories are allowed')
        yield descriptor if descriptor is not None else directory
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


class SessionLog:
    def __init__(self, runs_root: Path, run_id: str, metadata: dict | None = None, *, recovery: bool = False):
        self.run_id = validate_run_id(run_id)
        self.run_dir = Path(runs_root).resolve() / run_id
        self.events_path = self.run_dir / "events.jsonl"
        self.session_path = self.run_dir / "session.json"
        self.artifacts_dir = self.run_dir / "artifacts"
        self._lock = threading.RLock()
        self._closed = False
        self.failed = False
        self._listeners: set = set()
        if recovery:
            if not self.session_path.is_file():
                raise LogFailure("Session metadata is missing")
            self.metadata = json.loads(self.session_path.read_text(encoding="utf-8"))
            if self.metadata.get("run_id") != run_id:
                raise LogFailure("Session metadata run_id mismatch")
            self.artifacts_dir.mkdir(exist_ok=True)
            self._seq = self._scan_recovery_events()
            self.append("recovery_opened", {"previous_phase": self.metadata.get("phase")})
        else:
            if self.run_dir.exists():
                raise LogFailure("Session already exists")
            self.run_dir.mkdir(parents=True)
            self.artifacts_dir.mkdir()
            self._seq = 0
            self.metadata = {"run_id": run_id, "created_at": time.time(), **(metadata or {})}
            if self.metadata["run_id"] != run_id:
                raise ValueError("Metadata run_id mismatch")
            self._write_metadata()
            self.events_path.touch()
        self.artifacts_dir.mkdir(exist_ok=True)

    def _scan_recovery_events(self) -> int:
        if not self.events_path.exists():
            self.events_path.touch()
            return 0
        last_seq = 0
        with self.events_path.open("rb") as stream:
            while True:
                good_end = stream.tell()
                line = stream.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                    if (not isinstance(item, dict) or item.get("run_id") != self.run_id
                            or type(item.get("seq")) is not int or item["seq"] != last_seq + 1):
                        raise ValueError("Event identity or sequence mismatch")
                except (ValueError, UnicodeDecodeError) as exc:
                    tail = line + stream.read()
                    if b"\n" in tail.rstrip(b"\n"):
                        raise LogFailure("Corrupt event in the middle of the log") from exc
                    self.preserve_bytes("recovery/partial-tail.bin", tail)
                    with self.events_path.open("r+b") as writer:
                        writer.truncate(good_end)
                        writer.flush()
                        os.fsync(writer.fileno())
                    break
                last_seq = item["seq"]
        return last_seq

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def _write_metadata(self) -> None:
        temp_path = self.run_dir / f"session-{uuid.uuid4().hex}.tmp"
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.metadata, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            # On Windows a concurrent read briefly denies replacement of the
            # destination. Keep the new complete file and retry only sharing
            # errors; a persistent failure still fails the session writer.
            for attempt in range(40):
                try:
                    os.replace(temp_path, self.session_path)
                    break
                except PermissionError as exc:
                    if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or attempt == 39:
                        raise
                    time.sleep(0.025)
        except Exception as exc:
            self.failed = True
            raise LogFailure(f"Metadata write failed: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def update(self, **fields) -> dict:
        with self._lock:
            self._require_open()
            if "run_id" in fields and fields["run_id"] != self.run_id:
                raise ValueError("Cannot change run_id")
            self.metadata = {**self.metadata, **fields}
            self._write_metadata()
            return dict(self.metadata)

    def append(self, kind: str, fields: dict | None = None) -> dict:
        with self._lock:
            self._require_open()
            if not isinstance(kind, str) or not kind:
                raise ValueError("Event kind is required")
            values = fields or {}
            if any(key in values for key in ("run_id", "seq", "kind", "at")):
                raise ValueError("Event identity fields are reserved")
            event = {"run_id": self.run_id, "seq": self._seq + 1, "kind": kind, "at": time.time(),
                     **values, **classify_event(kind, values)}
            try:
                with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception as exc:
                self.failed = True
                raise LogFailure(f"Event write failed: {exc}") from exc
            self._seq += 1
            for listener in list(self._listeners):
                try:
                    listener(event)
                except Exception:
                    self._listeners.discard(listener)
            return event

    def listen(self, callback) -> None:
        with self._lock:
            self._require_open()
            self._listeners.add(callback)

    def unlisten(self, callback) -> None:
        with self._lock:
            self._listeners.discard(callback)

    def preserve_bytes(self, name: str, data: bytes) -> dict:
        with self._lock:
            self._require_open()
            relative = safe_relative_path(name)
            if not isinstance(data, bytes):
                raise TypeError("Evidence must be bytes")
            saved_name = relative.with_name(f"{relative.stem}-{uuid.uuid4().hex}{relative.suffix}")
            path = self.artifacts_dir / saved_name
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if any(part.is_symlink() for part in (path.parent, *path.parents)):
                    raise ValueError("Artifact link is not allowed")
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                digest = hashlib.sha256(data).hexdigest()
                if path.stat().st_size != len(data) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise OSError("Evidence verification failed")
            except Exception as exc:
                self.failed = True
                raise LogFailure(f"Evidence write failed: {exc}") from exc
            return {"path": (Path("artifacts") / saved_name).as_posix(), "bytes": len(data), "sha256": digest}

    def preserve_file(self, name: str, source: Path) -> dict:
        return self.preserve_bytes(name, Path(source).read_bytes())

    def read_events(self, after_seq: int = 0, limit: int = 500) -> list[dict]:
        if not isinstance(after_seq, int) or after_seq < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor")
        if not self.events_path.exists():
            return []
        result = []
        with self.events_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event["run_id"] != self.run_id:
                    raise LogFailure("Event run_id mismatch")
                if event["seq"] > after_seq:
                    result.append(event_view(event))
                    if len(result) >= limit:
                        break
        return result

    def flush(self) -> None:
        with self._lock:
            self._require_open()
            try:
                for path in (self.events_path, self.session_path):
                    with path.open("r+b") as stream:
                        os.fsync(stream.fileno())
            except Exception as exc:
                self.failed = True
                raise LogFailure(f"Session flush failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._listeners.clear()

    def _require_open(self) -> None:
        if self._closed or self.failed:
            raise LogFailure("Session writer is closed or failed")


class SessionStore:
    """Read disk lifecycle state before admitting another run."""

    def __init__(self, runs_root: Path):
        self.root = Path(runs_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, run_id: str, metadata: dict) -> SessionLog:
        defaults = {"phase": "preparing", "run_result": None, "log_status": "collecting",
                    "cleanup_status": "pending", "lab_reset_status": "pending"}
        return SessionLog(self.root, run_id, {**defaults, **metadata})

    def read(self, run_id: str) -> dict:
        path = self.root / validate_run_id(run_id) / "session.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(run_id) from exc
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            raise LogFailure("Corrupt session metadata")
        return data

    def read_events(self, run_id: str, *, after_seq: int = 0, limit: int = 250) -> list[dict]:
        """Read persisted events without reopening a writer or altering the run."""
        run_id = validate_run_id(run_id)
        self.read(run_id)
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor")
        events = []
        with (self.root / run_id / "events.jsonl").open("r", encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if event.get("run_id") != run_id or type(event.get("seq")) is not int:
                    raise LogFailure("Event log identity mismatch")
                if event["seq"] > after_seq:
                    events.append(event_view(event))
                    if len(events) == limit:
                        break
        return events

    def repair_unowned_metadata(self, run_id: str) -> dict:
        """Rebuild a creation record only when no runtime or owned process remains.

        An unknown process owner is never inferred from a directory name.  A
        record with a runtime or an unmatched process start stays blocked.
        """
        run_id = validate_run_id(run_id)
        directory = self.root / run_id
        if not directory.is_dir() or (directory / "runtime").exists():
            raise LogFailure("Cannot repair metadata while runtime ownership is unknown")
        session_path = directory / "session.json"
        try:
            return self.read(run_id)
        except (KeyError, ValueError, LogFailure):
            pass
        outstanding = set()
        containers = set()
        result = None
        events_path = directory / "events.jsonl"
        if events_path.exists():
            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event.get("run_id") != run_id:
                        raise ValueError("Event run_id mismatch")
                    process = event.get("process") or {}
                    identity = (process.get("pid"), process.get("created_at"))
                    if event.get("kind") == "process_started":
                        outstanding.add(identity)
                    elif event.get("kind") == "process_stopped":
                        outstanding.discard(identity)
                    elif event.get("kind") == "sandbox_created":
                        containers.add(event.get("container"))
                    elif event.get("kind") == "sandbox_removed":
                        containers.discard(event.get("container"))
                    elif event.get("kind") in {"finalize_started", "run_finalized"}:
                        result = event.get("run_result") or result
            except (ValueError, UnicodeDecodeError, TypeError) as exc:
                raise LogFailure("Cannot establish process ownership from events") from exc
        if outstanding or containers:
            raise LogFailure("Cannot repair metadata with unconfirmed managed processes")
        if session_path.exists():
            artifact = directory / "artifacts" / "recovery"
            artifact.mkdir(parents=True, exist_ok=True)
            backup = artifact / f"session-metadata-{uuid.uuid4().hex}.bin"
            with backup.open("xb") as stream:
                stream.write(session_path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        metadata = {"run_id": run_id, "created_at": time.time(), "phase": "recovery_required",
                    "run_result": result or "interrupted", "cleanup_status": "recovery_required",
                    "log_status": "collecting", "lab_reset_status": "pending",
                    "runtime_creation_intent": False, "managed_processes": [],
                    "metadata_rebuilt": True}
        temporary = directory / f"session-repair-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, session_path)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata

    def list_runs(self) -> list[dict]:
        result = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not RUN_ID_PATTERN.fullmatch(directory.name):
                continue
            try:
                result.append(self.read(directory.name))
            except (KeyError, ValueError, LogFailure):
                result.append({"run_id": directory.name, "phase": "recovery_required",
                               "cleanup_status": "recovery_required", "metadata_error": True})
        return sorted(result, key=lambda item: item.get("created_at", 0), reverse=True)

    def can_start(self) -> tuple[bool, str]:
        for meta in self.list_runs():
            path = self.root / meta["run_id"]
            if meta.get("metadata_error"):
                return False, f"{meta['run_id']}: session metadata needs recovery"
            if ((path / "runtime").exists() or meta.get("phase") != "finished"
                    or meta.get("log_status") != "finalized"
                    or meta.get("cleanup_status") != "succeeded"
                    or meta.get("lab_reset_status") not in {"succeeded", "not_applicable"}):
                return False, f"{meta['run_id']}: session cleanup is incomplete"
        return True, ""


class ControllerLease:
    """OS process lock for the one local lab controller."""

    def __init__(self, runs_root: Path):
        root = Path(runs_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / ".controller.lock"
        self._stream = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        try:
            stream = self.path.open("a+b")
        except OSError:
            return False
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
