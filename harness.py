"""Ollama streaming transport and Command decoding, independent of run lifecycle."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx2 as httpx

from j1_policy import pack_j1_context
from model_settings import validate_model_settings
from model_providers import OpenRouterError, is_openrouter, openrouter_stream, strip_tool_call_framing
from planning import (COMMAND_SCHEMA, SYSTEM_PROMPT, SYSTEM_PROMPT_SHA256, SYSTEM_PROMPT_VERSION,
                      Command, normalize_command_inputs, validate_command)
from session_log import SessionLog


OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
_NUM_PREDICT_MAX = 65536   # mirrors the num_predict field ceiling in model_settings


class ModelFailure(RuntimeError):
    def __init__(self, kind: str, message: str, call_id: str = "", *, retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.call_id = call_id
        self.retryable = retryable


def decode_first_command(raw: str) -> tuple[object, str]:
    """Decode the first complete JSON value from the model output; return (value, trailing).

    Ollama honours the response grammar, but some OpenRouter tool-call formats append framing after
    the JSON object — a stray '}, a </tool_call> tag, or a second malformed call — which a strict
    json.loads rejects as "Extra data" and which, if tolerated loosely, would corrupt the code field.
    raw_decode parses the first balanced value and reports where it ended, so trailing noise is
    discarded cleanly. The trailing text is returned (not dropped silently) so the caller can log that
    salvage happened. A leading non-JSON prefix still raises, and is handled as a format error upstream.
    """
    value, end = json.JSONDecoder().raw_decode(raw.strip())
    return value, raw.strip()[end:].strip()


def _ollama_stream(payload: dict):
    timeout = httpx.Timeout(connect=5, read=120, write=10, pool=5)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        with client.stream("POST", OLLAMA_URL, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line and line.strip():
                    yield json.loads(line)


class Harness:
    def __init__(self, log: SessionLog, *, transport=None, max_calls: int = 40,
                 stop_requested=None, max_decode_attempts: int = 6,
                 max_transport_retries: int = 3, transport_retry_backoff: float = 0.5,
                 system_prompt: str = SYSTEM_PROMPT, system_prompt_version: str = SYSTEM_PROMPT_VERSION,
                 system_prompt_sha256: str = SYSTEM_PROMPT_SHA256, command_guard=None, report_reserve: int = 0):
        self.log = log
        self.run_id = log.run_id
        self.transport = transport or _model_stream
        self.system_prompt = system_prompt
        self.system_prompt_version = system_prompt_version
        self.system_prompt_sha256 = system_prompt_sha256
        self.command_guard = command_guard
        self.report_reserve = report_reserve
        self.max_calls = max_calls
        self.max_decode_attempts = max_decode_attempts
        self.max_transport_retries = max_transport_retries
        self.transport_retry_backoff = transport_retry_backoff
        self.stop_requested = stop_requested or (lambda: False)
        self.calls = 0
        self.last_prompt_tokens = None   # prompt_eval_count of the most recent model call (real num_ctx usage)
        self.total_cost = 0.0            # summed OpenRouter billed cost (USD) across this run's calls
        self.output_num_predict_floor = None   # raised, persisted output budget after a length-truncation

    def decide(self, run_id: str, context: dict, *, model: str, settings: dict,
               deadline: float | None = None) -> Command:
        if run_id != self.run_id:
            raise ValueError("Harness belongs to another run")
        selected = validate_model_settings(settings)
        j1_budget = int(selected["options"].get("num_ctx", 8192) * 3.3 * 0.65) - len(self.system_prompt)
        if self.system_prompt_version == "J1":
            try:
                context = pack_j1_context(context, j1_budget)
            except ValueError as exc:
                raise ModelFailure("context", str(exc)) from exc
        report_only = self.report_reserve > 0 and bool(context.get("remaining", {}).get("finish_soon"))
        call_limit = (min(self.max_calls, self.calls + self.report_reserve) if report_only
                      else self.max_calls - self.report_reserve)
        options = dict(selected["options"])
        # Persist a raised output budget across steps: once a length-truncation forced num_predict up,
        # keep at least that floor so later steps do not truncate at the original cap again.
        if self.output_num_predict_floor is not None and options.get("num_predict", -1) != -1:
            options["num_predict"] = max(int(options.get("num_predict", 0)), self.output_num_predict_floor)
        payload = {"model": model, "stream": True, "format": COMMAND_SCHEMA,
                   "options": options,
                   "messages": [{"role": "system", "content": self.system_prompt},
                                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
        if selected["think"] is not None:
            payload["think"] = selected["think"]
        if "keep_alive" in selected:
            payload["keep_alive"] = selected["keep_alive"]
        last_error: ModelFailure | None = None
        transport_retries = 0
        # Retry recoverable format/output errors with feedback instead of failing the whole run
        # after a single retry. The global model-call budget below is the real ceiling.
        for _ in range(self.max_decode_attempts):
            if self.calls >= call_limit:
                raise ModelFailure("budget", "Model call budget exhausted")
            if self.stop_requested():
                raise ModelFailure("cancelled", "Run stopped")
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelFailure("timed_out", "Run deadline reached")
            self.calls += 1
            call_id = f"CALL-{self.calls:04d}"
            started = time.monotonic()
            self.log.append("model_request", {"call_id": call_id, "input": payload,
                                              "settings": selected, "attempt": self.calls,
                                              "system_prompt_version": self.system_prompt_version,
                                              "system_prompt_sha256": self.system_prompt_sha256})
            content: list[str] = []
            thinking: list[str] = []
            completion: dict = {}
            raw = ""
            try:
                for chunk in self.transport(payload):
                    if self.stop_requested():
                        raise ModelFailure("cancelled", "Run stopped", call_id)
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ModelFailure("timed_out", "Run deadline reached", call_id)
                    if chunk.get("error"):
                        raise ModelFailure("transport", str(chunk["error"])[:300], call_id)
                    message = chunk.get("message") or {}
                    for channel, pieces in (("thinking", thinking), ("content", content)):
                        delta = message.get(channel) or ""
                        if delta:
                            pieces.append(delta)
                            self.log.append("model_delta", {"call_id": call_id, "channel": channel, "text": delta})
                    if chunk.get("done"):
                        completion = {key: chunk[key] for key in (
                            "done_reason", "eval_count", "prompt_eval_count", "total_duration",
                            "eval_duration", "prompt_eval_duration", "load_duration", "cost") if key in chunk}
                raw = "".join(content)
                self.log.append("model_response", {"call_id": call_id, "output": raw,
                                                   "thinking": "".join(thinking),
                                                   "elapsed": round(time.monotonic() - started, 3), **completion})
                if completion.get("prompt_eval_count") is not None:
                    self.last_prompt_tokens = completion["prompt_eval_count"]
                if completion.get("cost") is not None:
                    self.total_cost += completion["cost"]
                if completion.get("done_reason") == "length":
                    limit = payload["options"].get("num_predict", "model default")
                    raise ModelFailure("output_limit", f"Model reached the output limit (num_predict={limit}, "
                                       f"eval_count={completion.get('eval_count', 'unknown')}, done_reason=length); "
                                       "the response was not executed", call_id)
                if not raw.strip():
                    reason = completion.get("done_reason", "unknown")
                    raise ModelFailure("format", f"Empty model content (done_reason={reason}); check num_predict/thinking", call_id)
                try:
                    answer, trailing = decode_first_command(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ModelFailure("format", f"Invalid JSON Command: {exc}", call_id) from exc
                if trailing:
                    self.log.append("command_salvaged", {"call_id": call_id, "trailing_chars": len(trailing),
                                                          "trailing_snippet": trailing[:80]})
                # Some OpenRouter models leak tool-call framing into field values; strip it before
                # validating/executing. Provider-specific knowledge stays in the provider module.
                if is_openrouter(model) and isinstance(answer, dict):
                    answer, sanitized_fields = strip_tool_call_framing(answer)
                    if sanitized_fields:
                        self.log.append("command_sanitized", {"call_id": call_id, "fields": sanitized_fields})
                try:
                    answer, normalized_fields = normalize_command_inputs(answer)
                    if normalized_fields:
                        self.log.append("command_normalized", {"call_id": call_id, "action": answer.get("action"),
                                                               "fields": normalized_fields})
                    command = validate_command(answer)
                    if report_only and command.action != "finish":
                        raise ValueError("Report reserve is active: return action finish; do not execute more tools")
                    if self.command_guard is not None:
                        self.command_guard(command, report_only)
                except (ValueError, TypeError) as exc:
                    raise ModelFailure("format", f"Invalid Command: {exc}", call_id) from exc
                self.log.append("command_decoded", {"call_id": call_id, "action": command.action,
                                                    "plan": command.plan, "reason": command.reason})
                return replace(command, call_id=call_id)
            except ModelFailure as exc:
                self.log.append("model_error", {"call_id": call_id, "error_kind": exc.kind,
                                                "message": str(exc), "elapsed": round(time.monotonic() - started, 3)})
                last_error = exc
                if exc.kind not in {"format", "output_limit"} or self.calls >= call_limit:
                    raise
                # Keep the failed answer available for correction without allowing a large
                # response to consume the entire context. The full raw response is logged.
                previous = raw if len(raw) <= 6000 else raw[:4000] + "\n[response middle omitted]\n" + raw[-2000:]
                retry_options = dict(payload["options"])
                if exc.kind == "output_limit":
                    # Reasoning models can spend the whole output budget thinking and never emit the
                    # command; re-sending the same cap just truncates again. Raise num_predict and keep
                    # the higher floor for the rest of the run so later steps have room too.
                    current = int(retry_options.get("num_predict", 0))
                    if current != -1 and current < _NUM_PREDICT_MAX:
                        raised = min(max(current, 1) * 2, _NUM_PREDICT_MAX)
                        retry_options["num_predict"] = raised
                        self.output_num_predict_floor = raised
                        self.log.append("output_budget_raised", {"call_id": call_id,
                                        "from_num_predict": current, "to_num_predict": raised})
                    feedback = ("The previous response hit the output limit while thinking and never produced "
                                "the command. You now have a larger output budget (num_predict="
                                f"{retry_options.get('num_predict')}). Think briefly, then output one complete JSON Command.")
                else:
                    feedback = ("The previous response was invalid: " + str(exc)[:220]
                                + ". Return one short, complete JSON Command. Use only the payload for its action: "
                                  "run_python uses code; run_command uses command; write_file uses path/content; "
                                  "read_file uses path. Every action including finish requires nonempty plan and reason. "
                                  "Unused path/content/command/code fields must be empty strings. "
                                  "Produce one small step with concise code, without explanatory comments.")
                if self.system_prompt_version == "J1":
                    previous = raw[:500] + ("\n[invalid output preview]" if len(raw) > 500 else "")
                    retry_feedback = [{"role": "assistant", "content": previous}, {"role": "user", "content": feedback}]
                    try:
                        context = pack_j1_context(context, j1_budget - sum(len(m["content"]) for m in retry_feedback))
                    except ValueError as error:
                        raise ModelFailure("context", str(error), call_id) from error
                    messages = [payload["messages"][0], {"role": "user", "content": json.dumps(context, ensure_ascii=False)}, *retry_feedback]
                else:
                    messages = [*payload["messages"], {"role": "assistant", "content": previous}, {"role": "user", "content": feedback}]
                payload = {**payload, "options": retry_options, "messages": messages}
            except (OpenRouterError, httpx.HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
                failure = ModelFailure("transport", str(exc)[:300], call_id,
                                       retryable=getattr(exc, "retryable", True))
                self.log.append("model_error", {"call_id": call_id, "error_kind": failure.kind,
                                                "message": str(failure), "elapsed": round(time.monotonic() - started, 3)})
                last_error = failure
                transport_retries += 1
                # Transient provider/network errors (incomplete stream, 429, timeouts, dropped
                # connections) usually clear on a retry; auth/billing/permission failures do not, so
                # OpenRouterError marks those non-retryable. Back off briefly, bounded by attempts,
                # the call budget, the deadline and a stop request.
                retryable = failure.retryable
                if (not retryable or transport_retries > self.max_transport_retries
                        or self.calls >= call_limit or self.stop_requested()
                        or (deadline is not None and time.monotonic() >= deadline)):
                    raise failure from exc
                self.log.append("transport_retry", {"call_id": call_id, "attempt": transport_retries})
                time.sleep(min(self.transport_retry_backoff * 2 ** (transport_retries - 1), 4.0))
        assert last_error is not None
        raise last_error


def _model_stream(payload: dict):
    transport = openrouter_stream if is_openrouter(payload["model"]) else _ollama_stream
    yield from transport(payload)
