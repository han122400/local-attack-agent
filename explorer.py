"""Run lifecycle and Command loop; model transport and execution live elsewhere."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from command_runtime import CommandRuntime
from harness import Harness, ModelFailure
from lab import reset_lab
from model_settings import default_model_settings, validate_model_settings
from planning import SYSTEM_PROMPT
from session_log import ControllerLease, LogFailure, SessionLog, SessionStore, validate_run_id


TARGET = "http://127.0.0.1:3000/"
MODEL = "qwen3.5:2b"
RUNS_ROOT = Path(__file__).resolve().parent / "runs"
DEFAULT_BUDGETS = {"seconds": 1200, "model_calls": 40, "tool_calls": 80,
                   "command_seconds": 30, "finalize_seconds": 120}

# The full prompt (system + context) must fit num_ctx with room left for the model's output. We budget the
# CONTEXT (the user message) in chars — a token proxy, since tokens are only known after a call — after
# subtracting the system prompt and reserving ~35% of num_ctx for output. When the context exceeds this
# budget the oldest structured history is dropped and a context_compacted event is emitted.
_CHARS_PER_TOKEN = 3.3        # rough for mixed Korean/English/code
_PROMPT_FRACTION = 0.65       # share of num_ctx used for the prompt (rest reserved for output)


def context_char_budget(num_ctx: int) -> int:
    total_prompt_chars = int(max(1024, num_ctx) * _CHARS_PER_TOKEN * _PROMPT_FRACTION)
    return max(2000, total_prompt_chars - len(SYSTEM_PROMPT))


def _digest(text: str, limit: int = 240) -> str:
    """Head+tail digest so a long output keeps its start AND end, not only the trailing noise."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return (text[:head] + " … " + text[-(limit - head - 3):]).strip()


def compose_report(findings: list[dict], run_result: str, model_reason: str) -> str:
    """Deterministic report built from the preserved findings, so every run ends with an actual result
    (confirmed observations / inconclusive / failures) even when the model never produced a finish summary."""
    findings = findings or []
    evidence = [f for f in findings if f.get("evidence")]
    inconclusive = [f for f in findings if f.get("status") == "ok" and not f.get("evidence")]
    failed = [f for f in findings if f.get("status") in ("error", "timeout")]
    lines = [f"결과: {run_result} · 스텝 {len(findings)}개 "
             f"(증거 {len(evidence)} · 미확정 {len(inconclusive)} · 실패 {len(failed)})"]
    if model_reason:
        lines.append(f"모델 종료 요약: {model_reason}")
    if evidence:
        lines.append("확인된 관찰:")
        for f in evidence[:12]:
            lines.append(f"  - [{f['step']}] {f.get('plan', '')} → {str(f.get('observed', ''))[:140]}")
    else:
        lines.append("확인된(증거 있는) 관찰 없음.")
    if inconclusive:
        steps = ", ".join(str(f["step"]) for f in inconclusive[:8])
        lines.append(f"미확정(코드는 실행됐으나 증거 불충분): {len(inconclusive)}건 (스텝 {steps}).")
    if failed:
        steps = ", ".join(str(f["step"]) for f in failed[:8])
        lines.append(f"실패/타임아웃: {len(failed)}건 (스텝 {steps}).")
    return "\n".join(lines)


def safe_target(target: str, allowed_origin: str = TARGET) -> str:
    if not isinstance(target, str) or not target or any(char in target for char in "\x00\r\n\\"):
        raise ValueError("Invalid target URL")
    parsed = urlsplit(target)
    allowed = urlsplit(allowed_origin)
    if (parsed.scheme, parsed.hostname, parsed.port) != (allowed.scheme, allowed.hostname, allowed.port):
        raise ValueError("Target must be the authorized local lab")
    if parsed.username or parsed.password:
        raise ValueError("Target credentials are not allowed")
    return target


def validate_budgets(raw: dict | None) -> dict:
    values = {**DEFAULT_BUDGETS, **(raw or {})}
    if set(values) != set(DEFAULT_BUDGETS) or any(
        type(value) is not int or value < 1 or value > (3600 if key.endswith("seconds") else 500)
        for key, value in values.items()
    ):
        raise ValueError("Invalid run budgets")
    return values


class ExplorerV3:
    def __init__(self, runs_root: Path = RUNS_ROOT, *, transport=None, browser_enabled: bool = True,
                 lab_reset=None, allowed_target: str = TARGET):
        self.store = SessionStore(runs_root)
        self.lease = ControllerLease(runs_root)
        self.instance_id = uuid.uuid4().hex
        self.transport = transport
        self.browser_enabled = browser_enabled
        self.lab_reset = lab_reset or reset_lab
        self.allowed_target = allowed_target
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_run_id: str | None = None
        self._runtime: CommandRuntime | None = None
        self._subscribers: set[queue.Queue] = set()
        self._event_subscribers: set[queue.Queue] = set()
        self._recent_state: dict = {"status": "idle", "run_id": None, "console": "", "model": MODEL,
                                    "target": TARGET, "model_settings": default_model_settings()}

    def can_start(self) -> tuple[bool, str]:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False, "Another run is active or finalizing"
            if not self.lease.acquire():
                return False, "Another controller owns the local lab"
            try:
                return self.store.can_start()
            finally:
                self.lease.release()

    def start(self, target: str = TARGET, model: str = MODEL, *, goal: str = "Explore the local lab",
              model_settings: dict | None = None, scenario_source: str = "manual", chain_mode: str = "none",
              budgets: dict | None = None) -> str | bool:
        if scenario_source != "manual" or chain_mode != "none":
            raise ValueError("Only manual scenario with none chain is implemented")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError("Manual goal is required")
        target = safe_target(target, self.allowed_target)
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("Installed model name is required")
        selected = default_model_settings() if model_settings is None else validate_model_settings(model_settings)
        selected_budgets = validate_budgets(budgets)
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            if not self.lease.acquire():
                raise RuntimeError("Another controller owns the local lab")
            ready, reason = self.store.can_start()
            if not ready:
                self.lease.release()
                raise RuntimeError(reason)
            self.stop_event.clear()
            run_id = uuid.uuid4().hex
            metadata = {"owner_instance_id": self.instance_id, "controller_pid": os.getpid(),
                        "runtime_path": str(self.store.root / run_id / "runtime"),
                        "runtime_creation_intent": False, "goal": goal.strip(), "target": target,
                        "model": model, "model_settings": selected, "budgets": selected_budgets,
                        "scenario_source": scenario_source, "chain_mode": chain_mode,
                        "started_at": time.time(), "finished_at": None, "finish_reason": "",
                        "managed_processes": []}
            try:
                log = self.store.create(run_id, metadata)
                log.append("run_created", {"owner_instance_id": self.instance_id, "goal": goal.strip()})
            except Exception:
                self.lease.release()
                raise
            self.active_run_id = run_id
            self._recent_state = {**metadata, "run_id": run_id, "status": "running", "phase": "preparing",
                                  "console": "", "events": []}
            log.listen(self._broadcast)
            self.worker = threading.Thread(target=self._thread_main, args=(run_id, log), daemon=True)
            self.worker.start()
            return run_id

    def _broadcast(self, event: dict) -> None:
        with self.lock:
            if event.get("run_id") != self.active_run_id:
                return
            for subscriber in list(self._event_subscribers):
                if subscriber.full():
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                subscriber.put_nowait(event)
            if event["kind"] in {"model_delta", "exec_output"}:
                chunk = event.get("text", "")
                self._recent_state["console"] = (self._recent_state.get("console", "") + chunk)[-60000:]
                for subscriber in list(self._subscribers):
                    if subscriber.full():
                        try:
                            subscriber.get_nowait()
                        except queue.Empty:
                            pass
                    subscriber.put_nowait(("delta", chunk))

    def _thread_main(self, run_id: str, log: SessionLog) -> None:
        result = "error"
        reason = ""
        runtime = CommandRuntime(self.store.root, log)
        with self.lock:
            self._runtime = runtime
        try:
            if not any(item.get("lab_reset_status") == "succeeded" and item.get("phase") == "finished"
                       for item in self.store.list_runs() if item.get("run_id") != run_id):
                log.update(initial_lab_status="running")
                initial_probe = self.lab_reset()
                if not isinstance(initial_probe, dict) or not initial_probe.get("verified"):
                    raise RuntimeError("Initial lab reset probe failed")
                log.append("initial_lab_reset", {"probe": initial_probe})
                log.update(initial_lab_status="succeeded", initial_lab_probe=initial_probe)
            log.update(phase="running")
            runtime.start_session(run_id, target=log.metadata["target"], browser=self.browser_enabled)
            result, reason = self._loop(run_id, log, runtime)
        except LogFailure as exc:
            result, reason = "error", f"Log failure: {exc}"
        except ModelFailure as exc:
            result = "stopped" if exc.kind == "cancelled" else "timed_out" if exc.kind == "timed_out" else "error"
            reason = f"Model {exc.kind}: {exc}"
        except Exception as exc:
            result, reason = "error", f"Executor or lifecycle failure: {exc}"
            try:
                log.append("run_error", {"error_kind": "executor", "message": str(exc)[:500],
                                         "exception_type": type(exc).__name__})
            except LogFailure:
                pass
        finally:
            if self.stop_event.is_set() and result == "completed":
                result, reason = "stopped", "User stopped the run"
            finalizer = threading.Thread(target=self._finalize, args=(run_id, log, runtime, result, reason), daemon=False)
            finalizer.start()
            finalizer.join()
            with self.lock:
                self._runtime = None
                self.active_run_id = None
            self.lease.release()

    def _loop(self, run_id: str, log: SessionLog, runtime: CommandRuntime) -> tuple[str, str]:
        metadata = log.metadata
        budgets = metadata["budgets"]
        deadline = time.monotonic() + budgets["seconds"]
        harness = Harness(log, transport=self.transport, max_calls=budgets["model_calls"],
                          stop_requested=self.stop_event.is_set)
        num_ctx = (metadata.get("model_settings", {}).get("options", {}) or {}).get("num_ctx", 8192)
        budget = context_char_budget(num_ctx)
        recent: list[dict] = []
        history: list[dict] = []       # structured memory of every step; survives recent trimming
        seen: dict[str, int] = {}      # observation digest -> step that first produced it (repetition)
        durations: list[float] = []    # recent decide() wall times, to reserve time for a final summary
        for tool_index in range(budgets["tool_calls"]):
            if self.stop_event.is_set():
                log.update(findings=history)
                return "stopped", "User stopped the run"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.update(findings=history)
                return "timed_out", "Run time budget exhausted"
            avg_call = sum(durations[-3:]) / len(durations[-3:]) if durations else 45.0
            finish_soon = remaining < max(avg_call * 1.5, 45)

            def build_context(window: int) -> dict:
                ctx = {"goal": metadata["goal"], "target": metadata["target"],
                       "runtime_path": str(runtime.runtime_dir), "python": runtime.python,
                       "browser_api": "run_python has page/context/browser and helpers get_text/get_json/"
                                      "http_get/goto_and_render/list_inputs/submit_input; top-level await.",
                       # history = structured memory of all prior steps (avoid forgetting/repeating);
                       # recent = fuller detail for the latest steps.
                       "history": history[-window:], "recent": recent[-2:],
                       "remaining": {"seconds": max(0, int(remaining)),
                                     "model_calls": budgets["model_calls"] - harness.calls,
                                     "tool_calls": budgets["tool_calls"] - tool_index,
                                     "avg_call_seconds": round(avg_call, 1), "finish_soon": finish_soon}}
                return ctx

            # Compaction: keep the assembled context under a char budget (token proxy) by dropping the
            # oldest structured history first; emit an event and expose usage so the UI can show it.
            window = min(len(history), 30)
            context = build_context(window)
            context_chars = len(json.dumps(context, ensure_ascii=False))
            while context_chars > budget and window > 1:
                window -= 1
                context = build_context(window)
                context_chars = len(json.dumps(context, ensure_ascii=False))
            dropped = min(len(history), 30) - window
            if dropped > 0:
                log.append("context_compacted", {"dropped": dropped, "kept_steps": window, "chars": context_chars})
            # Expose both the char budget (pre-call proxy) and the real token usage of the last call.
            usage = {"context_chars": context_chars, "context_budget": budget,
                     "prompt_tokens": harness.last_prompt_tokens, "num_ctx": num_ctx}
            self._recent_state.update(**usage)
            log.update(**usage)
            if history and history[-1].get("repeat_of"):
                context["note"] = ("Your last step repeated an earlier observation with no new evidence. "
                                   "Try a different route, feature, or approach — do not repeat it.")
            if finish_soon:
                context["instruction"] = ("Time is almost up. Call finish now with the actual result: the "
                                          "confirmed facts (each with its evidence), the failures, and what "
                                          "remains unverified, from history and recent. No new exploration.")
            started = time.monotonic()
            command = harness.decide(run_id, context, model=metadata["model"],
                                     settings=metadata["model_settings"], deadline=deadline)
            durations.append(time.monotonic() - started)
            log.update(plan=command.plan, reason=command.reason, last_call_id=command.call_id)
            if command.action == "finish":
                log.append("run_finished_by_model", {"call_id": command.call_id, "reason": command.reason,
                                                     "plan": command.plan})
                log.update(findings=history)
                return "completed", command.reason
            execution = runtime.run(run_id, command.runtime_payload(), call_id=command.call_id,
                                    timeout=budgets["command_seconds"])
            record = asdict(execution)
            record["stdout"] = record["stdout"][-1500:]
            record["stderr"] = record["stderr"][-800:]
            record["exception"] = record["exception"][-800:]
            recent.append({"call_id": command.call_id, "plan": command.plan, "reason": command.reason,
                           "command": {"action": command.action, "path": command.path,
                                       "command": command.command, "code": command.code[-800:]},
                           "result": record})
            # Structured memory: keep the model's claim (plan/reason) separate from the actual observation,
            # and code-success (status) separate from investigation-success (evidence present).
            if execution.timed_out:
                status, observed, error = "timeout", "", "code timed out"
            elif execution.exit_code == 0:
                status, observed, error = "ok", _digest(execution.stdout), ""
            else:
                status, observed, error = "error", "", _digest(execution.exception or execution.stderr, 200)
            repeat_of = seen.get(observed) if observed else None
            if observed and repeat_of is None:
                seen[observed] = tool_index + 1
            entry = {"step": tool_index + 1, "action": command.action, "plan": command.plan[:140],
                     "claim": command.reason[:200], "status": status,
                     "observed": observed or "(no evidence)", "error": error,
                     "evidence": bool(observed) and status == "ok"}
            if repeat_of is not None:
                entry["repeat_of"] = repeat_of
            history.append(entry)
            log.update(last_exec_id=execution.exec_id, tool_calls=tool_index + 1, findings=history)
        log.update(findings=history)
        return "error", "Tool call budget exhausted"

    def _finalize(self, run_id: str, log: SessionLog, runtime: CommandRuntime,
                  run_result: str, reason: str) -> None:
        configured = log.metadata.get("budgets")
        configured = configured if isinstance(configured, dict) else {}
        budget = configured.get("finalize_seconds", DEFAULT_BUDGETS["finalize_seconds"])
        budget = budget if type(budget) is int and budget > 0 else DEFAULT_BUDGETS["finalize_seconds"]
        deadline = time.monotonic() + budget
        # Deterministic report from the preserved findings, so a timed-out/stopped run still ends with an
        # actual result instead of only a deadline message. Best-effort; the full record stays on disk.
        try:
            report = compose_report(log.metadata.get("findings") or [], run_result, reason)
            log.update(report=report)
            self._recent_state.update(report=report)
        except LogFailure:
            pass
        def remaining(cap: float) -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Finalization time budget exhausted")
            return min(cap, left)
        try:
            log.update(run_result=run_result, finish_reason=reason, phase="finalizing")
            log.append("finalize_started", {"run_result": run_result, "reason": reason})
            if log.metadata.get("cleanup_status") != "succeeded" or runtime.inspect(run_id)["runtime_exists"] or runtime.inspect(run_id)["managed_processes"]:
                log.update(cleanup_status="running")
                runtime.stop(run_id, remaining(20))
                if runtime.inspect(run_id)["managed_processes"]:
                    raise RuntimeError("Managed processes remain after stop")
                log.append("cleanup_step", {"step": "stop", "status": "succeeded"})
                if log.metadata.get("log_status") not in {"preserved", "finalized"}:
                    if log.metadata.get("log_status") == "failed" and log.metadata.get("runtime_creation_intent") and not runtime.inspect(run_id)["runtime_exists"]:
                        raise RuntimeError("Evidence source is missing after log failure")
                    remaining(120)
                    runtime.export_evidence(run_id)
                    remaining(120)
                    log.update(log_status="preserved")
                    log.append("cleanup_step", {"step": "evidence", "status": "succeeded"})
                runtime.destroy(run_id, remaining(20))
                if runtime.inspect(run_id)["runtime_exists"]:
                    raise RuntimeError("Runtime folder remains after destroy")
                log.update(cleanup_status="succeeded")
                log.append("cleanup_step", {"step": "destroy", "status": "succeeded"})
            if log.metadata.get("lab_reset_status") != "succeeded":
                log.update(phase="resetting", lab_reset_status="running")
                remaining(120)
                probe = self.lab_reset()
                remaining(120)
                if not isinstance(probe, dict) or not probe.get("verified"):
                    raise RuntimeError("Lab reset probe failed")
                log.append("lab_reset", {"probe": probe, "status": "succeeded"})
                log.update(lab_reset_status="succeeded")
            log.append("run_finalized", {"run_result": run_result, "reason": reason})
            remaining(120)
            log.flush()
            remaining(120)
            log.update(phase="finished", log_status="finalized", cleanup_error="", finished_at=time.time())
            with self.lock:
                self._recent_state.update(status=run_result, phase="finished", finish_reason=reason,
                                          finished_at=log.metadata["finished_at"])
        except Exception as exc:
            # A failed writer must still stop owned processes, while retaining
            # runtime files for an explicit recovery attempt.
            if log.failed or isinstance(exc, LogFailure):
                try:
                    runtime.stop(run_id, 20, record=False)
                except Exception:
                    pass
            try:
                failed = {"phase": "recovery_required", "finish_reason": reason,
                          "cleanup_error": str(exc)[:500]}
                if runtime.inspect(run_id)["runtime_exists"] or runtime.inspect(run_id)["managed_processes"]:
                    failed["cleanup_status"] = "failed"
                elif log.metadata.get("cleanup_status") != "succeeded":
                    failed["cleanup_status"] = "failed"
                if log.metadata.get("log_status") != "preserved" and log.metadata.get("log_status") != "finalized":
                    failed["log_status"] = "failed"
                if log.metadata.get("phase") == "resetting" or log.metadata.get("lab_reset_status") == "running":
                    failed["lab_reset_status"] = "failed"
                log.append("finalize_failed", {"error": str(exc)[:500], "exception_type": type(exc).__name__,
                                               "component": "lab" if log.metadata.get("phase") == "resetting" else "session"})
                log.update(**failed)
            except Exception:
                pass
            with self.lock:
                self._recent_state.update(status="cleanup_failed", phase="recovery_required",
                                          finish_reason=reason, cleanup_error=str(exc)[:500])
        finally:
            log.close()

    def stop(self, run_id: str | None = None) -> bool:
        with self.lock:
            if not self.active_run_id or (run_id is not None and run_id != self.active_run_id):
                return False
            self.stop_event.set()
            if self._runtime:
                self._runtime.cancel()
            self._recent_state.update(status="stopping", phase="stopping")
            return True

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self._recent_state, ensure_ascii=False))

    def subscribe(self) -> tuple[queue.Queue, str]:
        subscriber: queue.Queue = queue.Queue(maxsize=1024)
        with self.lock:
            self._subscribers.add(subscriber)
            return subscriber, self._recent_state.get("console", "")

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.lock:
            self._subscribers.discard(subscriber)

    def subscribe_events(self, run_id: str) -> queue.Queue | None:
        validate_run_id(run_id)
        with self.lock:
            if self.active_run_id != run_id or self.worker is None or not self.worker.is_alive():
                return None
            subscriber: queue.Queue = queue.Queue(maxsize=2048)
            self._event_subscribers.add(subscriber)
            return subscriber

    def unsubscribe_events(self, subscriber: queue.Queue) -> None:
        with self.lock:
            self._event_subscribers.discard(subscriber)

    def get_run(self, run_id: str) -> dict:
        return self.store.read(validate_run_id(run_id))

    def recover(self, run_id: str) -> bool:
        validate_run_id(run_id)
        with self.lock:
            if self.worker and self.worker.is_alive():
                return False
            if not self.lease.acquire():
                raise RuntimeError("Another controller owns the local lab")
            try:
                try:
                    metadata = self.store.read(run_id)
                except (KeyError, ValueError, LogFailure):
                    metadata = self.store.repair_unowned_metadata(run_id)
                if (metadata.get("phase") == "finished" and metadata.get("cleanup_status") == "succeeded"
                        and metadata.get("log_status") == "finalized" and metadata.get("lab_reset_status") == "succeeded"):
                    self.lease.release()
                    return False
                log = SessionLog(self.store.root, run_id, recovery=True)
                result = metadata.get("run_result") or "interrupted"
                reason = metadata.get("finish_reason") or "App exited during the run"
                attempts = int(metadata.get("recovery_attempts", 0)) + 1
                cleanup = "succeeded" if metadata.get("cleanup_status") == "succeeded" else "recovery_required"
                log.update(run_result=result, cleanup_status=cleanup, phase="recovery_required",
                           recovery_attempts=attempts, recovery_owner_instance_id=self.instance_id)
                log.append("recovery_started", {"attempt": attempts, "previous_phase": metadata.get("phase")})
                self.stop_event.clear()
                self.active_run_id = run_id
                self._recent_state = {**log.metadata, "status": "recovery_required", "console": ""}
                log.listen(self._broadcast)
                self.worker = threading.Thread(target=self._recover_thread,
                                               args=(run_id, log, result, reason), daemon=True)
                self.worker.start()
                return True
            except Exception:
                self.lease.release()
                raise

    def _recover_thread(self, run_id: str, log: SessionLog, result: str, reason: str) -> None:
        runtime = CommandRuntime(self.store.root, log)
        try:
            runtime.restore_owned(run_id, log.metadata.get("managed_processes", []))
            with self.lock:
                self._runtime = runtime
            self._finalize(run_id, log, runtime, result, reason)
        finally:
            with self.lock:
                self._runtime = None
                self.active_run_id = None
            self.lease.release()


# Temporary import compatibility while the HTTP routes migrate in step 4.
V2Explorer = ExplorerV3
