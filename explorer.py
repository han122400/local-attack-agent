"""Run lifecycle and Command loop; model transport and execution live elsewhere."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from command_runtime import CommandRuntime, bounded_preview
from harness import Harness, ModelFailure
from j1_policy import J1Policy, pack_j1_context
from j2_policy import J2Policy, MEMORY_PATH, pack_j2_context
from lab import reset_lab
from model_settings import default_model_settings, validate_model_settings
from planning import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION, available_prompt_versions, load_prompt
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


def context_char_budget(num_ctx: int, system_prompt: str = SYSTEM_PROMPT) -> int:
    total_prompt_chars = int(max(1024, num_ctx) * _CHARS_PER_TOKEN * _PROMPT_FRACTION)
    return max(2000, total_prompt_chars - len(system_prompt))


def _digest(text: str, limit: int = 240) -> str:
    """Head+tail digest so a long output keeps its start AND end, not only the trailing noise."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return (text[:head] + " … " + text[-(limit - head - 3):]).strip()


def compose_report(findings: list[dict], run_result: str, model_reason: str, *, j1_state: dict | None = None,
                   j2_state: dict | None = None) -> str:
    """Deterministic report built from the preserved findings, so every run ends with an actual result
    (confirmed observations / inconclusive / failures) even when the model never produced a finish summary."""
    findings = findings or []
    evidence = [f for f in findings if f.get("evidence")]
    inconclusive = [f for f in findings if f.get("status") == "ok" and not f.get("evidence")]
    failed = [f for f in findings if f.get("status") in ("error", "timeout")]
    tracked = j1_state is not None or j2_state is not None
    lines = [f"결과: {run_result} · 스텝 {len(findings)}개 "
             f"({'실행 출력' if tracked else '증거'} {len(evidence)} · 미확정 {len(inconclusive)} · 실패 {len(failed)})"]
    if model_reason and j2_state is None:
        lines.append(f"모델 종료 요약{'(미검증 주장)' if tracked else ''}: {model_reason}")
    if evidence:
        lines.append("기록된 실행 출력(HTTP/취약점 미검증):" if tracked else "확인된 관찰:")
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
    partial = [f for f in findings if f.get("partial_observation")]
    if partial:
        lines.append("실패 단계의 부분 출력(미검증·전체 단계 성공을 의미하지 않음):")
        for item in partial[:8]:
            lines.append(f"  - [{item['step']}] {str(item['observed'])[:200]}")
    for item in findings:
        for blocked in item.get("blocked_requests", []):
            lines.append(f"하네스 요청 차단 [{item['step']}]: {blocked['method']} {blocked['url']} · {blocked['reason']} · 대상 사이트 오류의 증거가 아님")
    if j1_state:
        active_checks = {check: item for check, item in j1_state["coverage"].items()
                         if item["state"] != "not_started"}
        if active_checks:
            lines.append("J1 조사 기록(단계 실행 기록이며 취약점 확정/방어 입증이 아님):")
        for check, item in active_checks.items():
            lines.append(f"  - {check}: {item['state']} · 단계 {', '.join(item['stages']) or '없음'} · 스텝 {item['steps']}"
                         + (f" · 미검증 사유: {item['limits']}" if item['limits'] else ""))
            if item.get("basis"):
                lines.append(f"    후보 선택 근거(모델 진술·미검증): {item['basis']}")
        for item in j1_state.get("candidates", {}).values():
            lines.append(f"  - 후보 baseline={item['baseline_step']} · {item['check']}: {item['state']}"
                         f" · 단계 {item['stage_steps']} · 미검증 사유: {item['limits']}")
        for item in j1_state["request_memory"]:
            lines.append(f"  - 기준 코드 [{item['step']}]: {item['source_path']} · 필드 {item['form_fields']} · HTTP 성공은 미검증")
        if j1_state.get("unlinked_steps"):
            lines.append(f"추적 정보가 부족한 실행 단계: {j1_state['unlinked_steps']}")
    if j2_state is not None:
        lines.append("실행 기록 대조(HTTP 응답 메타데이터는 기존 도우미에 한정되며 보안 영향의 검증이 아님):")
        by_step = {item["step"]: item for item in findings}
        cited = {int(value) for value in re.findall(r"\[step:(\d{1,6})\]", model_reason or "")}
        missing = cited - by_step.keys()
        failed_citations = {step for step in cited & by_step.keys() if by_step[step]["status"] != "ok"}
        if missing:
            lines.append("존재하지 않는 단계 참조: " + ", ".join(map(str, sorted(missing))) + " · 근거로 사용할 수 없음.")
        if failed_citations:
            lines.append("실패 단계 참조: " + ", ".join(map(str, sorted(failed_citations))) + " · 해당 실패/부분 관찰만 증명하며 검사 완료의 근거가 아님.")
        if model_reason and not cited:
            lines.append("모델 보고서에 단계별 근거 참조가 없어 개별 주장은 대조되지 않음.")
        for item in j2_state.get("executions", [])[-12:]:
            lines.append(f"  - [step:{item['step']}] {item['action']} · {item['outcome']} · 실행 출력 {'있음' if item['output_present'] else '없음'}")
            for response in item.get("http_observations", []):
                lines.append(f"    응답 수신: {response['method']} {response['url']} · HTTP {response['status']} · 입력 처리/보안 영향은 미검증")
            for error in item.get("helper_errors", []):
                lines.append(f"    도우미 오류: {error['helper']} · {error['error_type']} · {error['url']} · 코드 종료 상태와 별개이며 해당 관찰의 성공을 뜻하지 않음")
        for item in j2_state.get("rejected_actions", []):
            lines.append(f"  - 미시행 {item['call_id']}: 사전 검사 거절 · {item['plan']} · {item['source_path']}")
        lines.append("실패·미시행의 계획은 수행 결과가 아님. HTTP 기록 부재만으로 미시행을 단정하지 않으며 원시 클라이언트·브라우저 요청은 이 기록에 모두 포함되지 않음.")
        lines.append(f"전체 조사 이력: {MEMORY_PATH} (런타임 내 경로; 종료 후 보존된 work 산출물 참조)")
        pipeline = j2_state.get("pipeline", {})
        review = pipeline.get("review", {})
        if review:
            lines.append(f"종료 검토: 선언 리소스 {review['declared_resources']} · 응답 메타데이터 {review['response_metadata_resources']} · 제공 텍스트 분석 {review['analyzed_resources']} · 분석 기록 없는 리소스 {review['unreviewed_resources']} · 제한된 분석 {review['limited_analyses']}")
            lines.append("응답은 쿼리를 제거한 경로 기준 대조이며, 텍스트 분석 기록은 전체·최신 본문 확보나 검증 완료를 뜻하지 않음. 남은 리소스의 방문은 강제하지 않으며 종료 범위의 한계로 남김.")
            for resource in pipeline.get("pending_resources", [])[:4]:
                lines.append(f"  - 분석 기록 없음: {resource['url']} · 선언 [step:{resource['declared_step']}]")
            if pipeline.get("omitted_resources") or pipeline.get("omitted_pending"):
                lines.append("리소스 목록 일부 생략: 전체 보존 범위와 생략 수는 조사 메모리 참조.")
        for failure in pipeline.get("failures", [])[-6:]:
            later = failure.get("later_completion_step")
            lines.append(f"오류 이력: {failure['kind']} · {failure['error_type']} · {failure['count']}건 · 단계 {failure['steps']}"
                         + (f" · 같은 공개 경로의 후속 도우미 완료 [step:{later}] (원인 해결의 입증 아님)" if later else " · 연결된 후속 완료 기록 없음"))
        summaries = j2_state.get("discovery_summaries", [])
        if summaries:
            lines.append("문서에서 추출해 보존한 단서(실제 접근·분석 완료나 입력 처리 확인을 뜻하지 않음):")
            for summary in summaries[-4:]:
                lines.append(f"  - 최근 분석 [step:{summary['step']}] {summary['source_url']} · 누적 보존 리소스 {len(summary['assets'])} · 입력 선언 {len(summary['surfaces'])} · 힌트 {len(summary['hints'])}"
                             + (" · 일부 생략됨" if summary['truncated'] else ""))
            if j2_state.get("omitted_discovery"):
                lines.append(f"이전 출처 {j2_state['omitted_discovery']}건은 보존 한도로 생략됨. 원본 실행 기록 참조.")
        lines.append("J2 후보 기록(모델 진술·미검증, closed는 조사 중단이며 안전 판정이 아님):")
        candidates = j2_state.get("candidates", [])
        if j2_state.get("omitted_notes"):
            lines.append(f"후보 기록 한도 초과: 메모 {j2_state['omitted_notes']}건은 실행 로그 참조. 조사 미완료.")
        if not candidates:
            lines.append("추적한 후보 없음. 실행 출력이나 후보 기록 부재는 취약점 부재를 뜻하지 않음.")
        for item in candidates:
            lines.append(f"  - [{item['step']}] {item['name']} · {item['state']}"
                         + (" · 실패 단계의 부분 기록" if item['partial'] else ""))
            if item['basis']:
                lines.append(f"    근거(모델 진술): {item['basis']}")
            level = {"hypothesis": "가설", "normal_behavior": "정상 동작 주장", "controlled_comparison": "통제 비교 주장",
                     "security_effect": "보안 영향 주장"}.get(item.get("evidence_level", "hypothesis"), "가설")
            lines.append(f"    근거 수준(모델 진술): {level} · 보안 영향의 독립 검증이 아님")
            if item.get("observation_steps"):
                lines.append(f"    참조 출력 있음: {item.get('available_observation_steps', [])} · 실패·현재·없는 단계 또는 출력 미확인: {item.get('unavailable_observation_steps', [])} · 출력의 의미·재현성은 미검증")
            if item['next_observation']:
                lines.append(f"    남은 확인: {item['next_observation']}")
            if item['evidence_paths']:
                lines.append(f"    참조(내용 미검증): {', '.join(item['evidence_paths'])}")
            sources = list(dict.fromkeys(path for path in (item.get('first_source_path'), item['source_path']) if path))
            if sources:
                lines.append(f"    실행 코드 참조: {', '.join(sources)}")
        lines.extend(j2_state.get("advisories", []))
        if model_reason:
            lines.append(f"모델 종료 요약(미검증 주장, 위 실행 기록과 구분): {model_reason}")
    return "\n".join(lines)


def safe_target(target: str, allowed_origin: str | None = None) -> str:
    """Validate a user-entered web target, optionally restricted to one origin."""
    if (not isinstance(target, str) or not target or len(target) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in target)):
        raise ValueError("Enter an absolute HTTP or HTTPS URL")
    try:
        parsed = urlsplit(target)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port == 0):
            raise ValueError("Target must be an absolute HTTP(S) URL without credentials")
        if allowed_origin is not None:
            allowed = urlsplit(allowed_origin)
            if (parsed.scheme, parsed.hostname, parsed.port) != (allowed.scheme, allowed.hostname, allowed.port):
                raise ValueError("Target must remain within the allowed origin")
    except ValueError as exc:
        raise ValueError(f"Invalid target URL: {exc}") from exc
    return target


def uses_juice_lab(target: str) -> bool:
    parsed = urlsplit(target)
    return (parsed.scheme, parsed.hostname, parsed.port) == ("http", "127.0.0.1", 3000)


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
                 lab_reset=None, allowed_target: str | None = None):
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
              budgets: dict | None = None, prompt_version: str | None = None) -> str | bool:
        if scenario_source != "manual" or chain_mode != "none":
            raise ValueError("Only manual scenario with none chain is implemented")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError("Manual goal is required")
        target = safe_target(target, self.allowed_target)
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("Installed model name is required")
        prompt_version = prompt_version or SYSTEM_PROMPT_VERSION
        if prompt_version not in available_prompt_versions():
            raise ValueError("Unknown system prompt version")
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
                        "lab_reset_status": "pending" if uses_juice_lab(target) else "not_applicable",
                        "initial_lab_status": "pending" if uses_juice_lab(target) else "not_applicable",
                        "model": model, "model_settings": selected, "budgets": selected_budgets,
                        "scenario_source": scenario_source, "chain_mode": chain_mode,
                        "prompt_version": prompt_version,
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
            if uses_juice_lab(log.metadata.get("target", TARGET)) and not any(
                       uses_juice_lab(item.get("target", TARGET))
                       and item.get("lab_reset_status") == "succeeded" and item.get("phase") == "finished"
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
        prompt_version = metadata.get("prompt_version", SYSTEM_PROMPT_VERSION)
        system_prompt, system_prompt_sha256 = load_prompt(prompt_version)
        policy = J1Policy(metadata["target"]) if prompt_version == "J1" else None
        notebook = J2Policy() if prompt_version == "J2" else None
        report_reserve = 1
        harness = Harness(log, transport=self.transport, max_calls=budgets["model_calls"],
                          stop_requested=self.stop_event.is_set, system_prompt=system_prompt,
                          system_prompt_version=prompt_version, system_prompt_sha256=system_prompt_sha256,
                          command_guard=policy.allow_action if policy else None, report_reserve=report_reserve)
        num_ctx = (metadata.get("model_settings", {}).get("options", {}) or {}).get("num_ctx", 8192)
        budget = context_char_budget(num_ctx, system_prompt)
        recent: list[dict] = []
        request_templates: list[dict] = []
        history: list[dict] = []       # structured memory of every step; survives recent trimming
        seen: dict[str, int] = {}      # observation digest -> step that first produced it (repetition)
        durations: list[float] = []    # recent decide() wall times, to reserve time for a final summary
        tool_index, report_required = 0, False
        if policy:
            log.update(j1_state=policy.snapshot())
        def save_notebook():
            if notebook is None:
                return
            snapshot = notebook.snapshot()
            log.update(j2_state=snapshot)
            try:
                runtime._file_path(MEMORY_PATH, writing=True).write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError) as exc:
                log.append("j2_memory_unavailable", {"reason": str(exc)[:160]})
        save_notebook()
        if notebook:
            def validate_j2_command(command, report_only):
                problems = notebook.command_problems(command)
                if not problems:
                    return
                source = f"tools/preflight-{command.call_id}.py"
                runtime._file_path(source, writing=True).write_text(command.code, encoding="utf-8")
                artifact = log.preserve_bytes(f"preflight/{command.call_id}.py", command.code.encode("utf-8"))
                notebook.record_rejection(command, problems, source, artifact["path"])
                log.append("preflight_rejected", {"call_id": command.call_id, "problems": problems,
                                                  "source_path": source, "artifact_path": artifact["path"]})
                save_notebook()
                raise ValueError(f"Preflight failed. Read {source}. " + "; ".join(problems[:2]))
            harness.command_guard = validate_j2_command
        while True:
            if self.stop_event.is_set():
                log.update(findings=history)
                return "stopped", "User stopped the run"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.update(findings=history)
                return "timed_out", "Run time budget exhausted"
            avg_call = sum(durations[-3:]) / len(durations[-3:]) if durations else 45.0
            finish_soon = (remaining < max(avg_call * 1.5, 45) or report_required
                           or harness.calls >= budgets["model_calls"] - report_reserve
                           or tool_index >= budgets["tool_calls"])

            def build_context(window: int) -> dict:
                ctx = {"goal": metadata["goal"], "target": metadata["target"],
                       "runtime_path": str(runtime.runtime_dir), "python": runtime.python,
                       "browser_api": "run_python has page/context/browser; top-level await. Await get_text/get_json/"
                                      "http_get/goto_and_render/list_inputs/submit_input/replay_request. "
                                      "http_get returns headers, body (up to 2000 chars), body_truncated and binary; "
                                      "get_text reads full text. goto_and_render returns a Page. "
                                      "captured_requests() lists recent browser form request IDs and exact field names. "
                                      "await replay_request(id, fields={'observed_name':'new_value'}) keeps other fields and uses current cookies. "
                                      "Do not treat blocked_requests as site errors or bypass scope guards.",
                       # history = structured memory of all prior steps (avoid forgetting/repeating);
                       # recent = fuller detail for the latest steps.
                       "history": history[-window:], "recent": recent[-2:],
                       "remaining": {"seconds": max(0, int(remaining)),
                                     "model_calls": budgets["model_calls"] - harness.calls,
                                     "tool_calls": budgets["tool_calls"] - tool_index,
                                     "avg_call_seconds": round(avg_call, 1), "finish_soon": finish_soon,
                                     "report_reserve": report_reserve}}
                if policy:
                    ctx["j1"] = policy.context()
                if notebook:
                    ctx["j2"] = notebook.context()
                    ctx["browser_api"] = {
                        "imports": "page/context/browser and helpers are already provided. tools exports helpers; browser exports page/context/browser. Individual helpers are functions, not modules.",
                        "unavailable_modules": sorted(notebook.unavailable_modules),
                        "http_get": {"call": "await http_get(url)", "returns": "dictionary: status, headers, body, body_truncated, binary, json?",
                                     "body_limit": 2_000_000},
                        "get_text": "await get_text(url) -> full text; no status dictionary",
                        "get_json": "await get_json(url) -> parsed JSON",
                        "page": "Playwright Page: await page.content() for HTML, await page.inner_text('body') for visible text",
                        "discover_inputs": "synchronous discover_inputs(body, source_url, content_type=''); no requests",
                        "note_candidate": "optional synchronous note_candidate; signature and claim levels in system prompt; notes are model statements only",
                        "request_replay": "captured_requests() is synchronous; await replay_request(id, fields={'observed_name':'value'}) preserves other fields",
                        "input_helpers": "await list_inputs/submit_input/compare_inputs; await goto_and_render returns a Page",
                        "example": "r = await http_get(target_origin + '/'); print(r['status']); print(discover_inputs(r['body'], r['url'], r['content_type']))",
                        "limits": "Python variables reset each call. Do not retry unavailable imports. Use standard-library modules or provided helpers. blocked_requests are harness limits, not target defenses."}
                if request_templates:
                    ctx["request_templates"] = request_templates
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
            # Expose both the char budget (pre-call proxy) and the real token usage of the last call,
            # plus the accumulated OpenRouter billed cost (USD; 0 for local Ollama runs).
            usage = {"context_chars": context_chars, "context_budget": budget,
                     "prompt_tokens": harness.last_prompt_tokens, "num_ctx": num_ctx,
                     "cost_usd": round(harness.total_cost, 6)}
            self._recent_state.update(**usage)
            log.update(**usage)
            if history and history[-1].get("repeat_of"):
                context["note"] = ("An observation digest repeated. This may be an intentional baseline/control/recheck; "
                                   "compare the actual candidate, input and response before deciding to change course. "
                                   "Reuse a working request instead of reconstructing it without new information." if policy or notebook else
                                   "Your last step repeated an earlier observation with no new evidence. "
                                   "Try a different route, feature, or approach — do not repeat it.")
            if finish_soon:
                context["instruction"] = ("The reporting phase is active. Call finish now with the actual result: the "
                                          "confirmed facts (each with its evidence), the failures, and what "
                                          "remains unverified, from history and recent. No new exploration.")
            if policy or notebook:
                try:
                    context = (pack_j2_context if notebook else pack_j1_context)(context, budget)
                except ValueError as exc:
                    raise ModelFailure("context", str(exc)) from exc
                log.update(context_chars=len(json.dumps(context, ensure_ascii=False)))
                self._recent_state["context_chars"] = log.metadata["context_chars"]
            started = time.monotonic()
            try:
                command = harness.decide(run_id, context, model=metadata["model"],
                                         settings=metadata["model_settings"], deadline=deadline)
            except ModelFailure as exc:
                if (exc.kind in {"budget", "format", "output_limit"}
                        or (exc.kind == "transport" and exc.retryable)):
                    if not finish_soon and harness.calls < budgets["model_calls"]:
                        report_required = True
                        log.append("report_requested", {"reason": str(exc)[:240], "calls_used": harness.calls})
                        continue
                    return "error", f"Model report unavailable ({exc.kind}); see the preserved observations and deterministic report."
                raise
            finally:
                log.update(findings=history, cost_usd=round(harness.total_cost, 6))
                if policy:
                    log.update(j1_state=policy.snapshot())
                durations.append(time.monotonic() - started)
            log.update(plan=command.plan, reason=command.reason, last_call_id=command.call_id)
            if command.action == "finish":
                log.append("run_finished_by_model", {"call_id": command.call_id, "reason": command.reason,
                                                     "plan": command.plan})
                log.update(findings=history)
                if notebook:
                    review = notebook.snapshot()["pipeline"]["review"]
                    log.update(finish_review=review)
                    log.append("finish_review", review)
                    status = notebook.investigation_status()
                    if status == "untracked" and (notebook.rejected_actions or any(
                            item["status"] in {"error", "timeout"} for item in history)
                            or any(item.get("helper_errors") for item in notebook.executions)):
                        status = "incomplete"
                    log.update(investigation_status=status)
                    if status == "incomplete" or not any(item["evidence"] for item in history):
                        return "incomplete", command.reason
                return "completed", command.reason
            command_timeout = budgets["command_seconds"]
            if policy or notebook:
                if self.stop_event.is_set():
                    return "stopped", "User stopped the run"
                left = deadline - time.monotonic()
                if left <= 0:
                    return "timed_out", "Run time budget exhausted before command execution"
                command_timeout = min(command_timeout, left)
            execution = runtime.run(run_id, command.runtime_payload(), call_id=command.call_id,
                                    timeout=command_timeout)
            record = asdict(execution)
            if execution.request_templates is not None:
                request_templates = execution.request_templates
            record.pop("request_templates", None)
            record.pop("investigation_notes", None)
            if notebook is not None:
                record.pop("discovery_summaries", None)
            if execution.path:
                try:
                    record["path"] = Path(execution.path).relative_to(runtime.runtime_dir).as_posix()
                except ValueError:
                    pass
            record["stdout"] = bounded_preview(record["stdout"], 1500)
            record["stderr"] = bounded_preview(record["stderr"], 800)
            record["exception"] = record["exception"][-800:]
            recent.append({"call_id": command.call_id, "plan": command.plan, "reason": command.reason,
                           "command": {"action": command.action, "path": command.path,
                                       "command": bounded_preview(command.command, 800),
                                       "code": bounded_preview(command.code, 800)},
                           "result": record})
            # Structured memory: keep the model's claim (plan/reason) separate from the actual observation,
            # and code-success (status) separate from investigation-success (evidence present).
            if execution.cancelled:
                status, observed, error = "error", _digest(execution.stdout), "execution cancelled"
            elif execution.timed_out:
                status, observed, error = "timeout", _digest(execution.stdout), "code timed out"
            elif execution.exit_code == 0:
                status, observed, error = "ok", _digest(execution.stdout), ""
            else:
                status, observed, error = "error", _digest(execution.stdout), _digest(execution.exception or execution.stderr, 200)
            repeat_of = seen.get(observed) if observed and status == "ok" else None
            if observed and status == "ok" and repeat_of is None:
                seen[observed] = tool_index + 1
            entry = {"step": tool_index + 1, "action": command.action, "plan": command.plan[:140],
                     "claim": command.reason[:200], "status": status,
                     "observed": observed or "(no evidence)", "error": error,
                     "evidence": bool(observed) and status == "ok"}
            if command.action == "run_python" and record["path"]:
                entry["source_path"] = record["path"]
            if observed and status != "ok":
                entry["partial_observation"] = True
            if execution.blocked_requests:
                entry["blocked_requests"] = execution.blocked_requests
            if execution.http_observations:
                entry["http_observations"] = execution.http_observations
            if execution.request_templates:
                entry["request_refs"] = [item["id"] for item in execution.request_templates
                                         if item["step"] == execution.exec_id]
            if repeat_of is not None:
                entry["repeat_of"] = repeat_of
            history.append(entry)
            if policy:
                policy.record(command, execution, tool_index + 1)
                log.update(j1_state=policy.snapshot())
            if notebook:
                notebook.record(execution, tool_index + 1, command)
                save_notebook()
            log.update(last_exec_id=execution.exec_id, tool_calls=tool_index + 1, findings=history)
            tool_index += 1

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
            report = compose_report(log.metadata.get("findings") or [], run_result, reason,
                                    j1_state=log.metadata.get("j1_state"), j2_state=log.metadata.get("j2_state"))
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
            if (uses_juice_lab(log.metadata.get("target", TARGET))
                    and log.metadata.get("lab_reset_status") != "succeeded"):
                log.update(phase="resetting", lab_reset_status="running")
                remaining(120)
                probe = self.lab_reset()
                remaining(120)
                if not isinstance(probe, dict) or not probe.get("verified"):
                    raise RuntimeError("Lab reset probe failed")
                log.append("lab_reset", {"probe": probe, "status": "succeeded"})
                log.update(lab_reset_status="succeeded")
            elif not uses_juice_lab(log.metadata.get("target", TARGET)):
                log.update(lab_reset_status="not_applicable")
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
                        and metadata.get("log_status") == "finalized"
                        and metadata.get("lab_reset_status") in {"succeeded", "not_applicable"}):
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
