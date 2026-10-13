"""Ollama streaming transport and Command decoding, independent of run lifecycle."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx2 as httpx

from model_settings import validate_model_settings
from planning import COMMAND_SCHEMA, SYSTEM_PROMPT, Command, normalize_command_inputs, validate_command
from session_log import SessionLog


OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


class ModelFailure(RuntimeError):
    def __init__(self, kind: str, message: str, call_id: str = ""):
        super().__init__(message)
        self.kind = kind
        self.call_id = call_id


def _ollama_stream(payload: dict):
    timeout = httpx.Timeout(connect=5, read=120, write=10, pool=5)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        with client.stream("POST", OLLAMA_URL, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line and line.strip():
                    yield json.loads(line)


class Harness:
    def __init__(self, log: SessionLog, *, transport=None, max_calls: int = 40, stop_requested=None):
        self.log = log
        self.run_id = log.run_id
        self.transport = transport or _ollama_stream
        self.max_calls = max_calls
        self.stop_requested = stop_requested or (lambda: False)
        self.calls = 0

    def decide(self, run_id: str, context: dict, *, model: str, settings: dict,
               deadline: float | None = None) -> Command:
        if run_id != self.run_id:
            raise ValueError("Harness belongs to another run")
        selected = validate_model_settings(settings)
        payload = {"model": model, "stream": True, "format": COMMAND_SCHEMA,
                   "options": selected["options"],
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
        if selected["think"] is not None:
            payload["think"] = selected["think"]
        if "keep_alive" in selected:
            payload["keep_alive"] = selected["keep_alive"]
        last_error: ModelFailure | None = None
        for _ in range(2):
            if self.calls >= self.max_calls:
                raise ModelFailure("budget", "Model call budget exhausted")
            if self.stop_requested():
                raise ModelFailure("cancelled", "Run stopped")
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelFailure("timed_out", "Run deadline reached")
            self.calls += 1
            call_id = f"CALL-{self.calls:04d}"
            started = time.monotonic()
            self.log.append("model_request", {"call_id": call_id, "input": payload,
                                              "settings": selected, "attempt": self.calls})
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
                            "eval_duration", "prompt_eval_duration", "load_duration") if key in chunk}
                raw = "".join(content)
                self.log.append("model_response", {"call_id": call_id, "output": raw,
                                                   "thinking": "".join(thinking),
                                                   "elapsed": round(time.monotonic() - started, 3), **completion})
                if completion.get("done_reason") == "length":
                    limit = selected["options"].get("num_predict", "model default")
                    raise ModelFailure("output_limit", f"Model reached the output limit (num_predict={limit}, "
                                       f"eval_count={completion.get('eval_count', 'unknown')}, done_reason=length); "
                                       "the response was not executed", call_id)
                if not raw.strip():
                    reason = completion.get("done_reason", "unknown")
                    raise ModelFailure("format", f"Empty model content (done_reason={reason}); check num_predict/thinking", call_id)
                try:
                    answer = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ModelFailure("format", f"Invalid JSON Command: {exc}", call_id) from exc
                try:
                    answer, normalized_fields = normalize_command_inputs(answer)
                    if normalized_fields:
                        self.log.append("command_normalized", {"call_id": call_id, "action": answer.get("action"),
                                                               "fields": normalized_fields})
                    command = validate_command(answer)
                except (ValueError, TypeError) as exc:
                    raise ModelFailure("format", f"Invalid Command: {exc}", call_id) from exc
                self.log.append("command_decoded", {"call_id": call_id, "action": command.action,
                                                    "plan": command.plan, "reason": command.reason})
                return replace(command, call_id=call_id)
            except ModelFailure as exc:
                self.log.append("model_error", {"call_id": call_id, "error_kind": exc.kind,
                                                "message": str(exc), "elapsed": round(time.monotonic() - started, 3)})
                last_error = exc
                if exc.kind not in {"format", "output_limit"} or self.calls >= self.max_calls:
                    raise
                # Keep the failed answer available for correction without allowing a large
                # response to consume the entire context. The full raw response is logged.
                previous = raw if len(raw) <= 6000 else raw[:4000] + "\n[response middle omitted]\n" + raw[-2000:]
                payload = {**payload, "messages": [*payload["messages"],
                    {"role": "assistant", "content": previous},
                    {"role": "user", "content": "The previous response was invalid: " + str(exc)[:220]
                     + ". Return one short, complete JSON Command. Use only the payload for its action: "
                       "run_python uses code; run_command uses command; write_file uses path/content; "
                       "read_file uses path. Every action including finish requires nonempty plan and reason. "
                       "Unused path/content/command/code fields must be empty strings. "
                       "Produce one small step with concise code, without explanatory comments, within the same output limit."}]}
            except (httpx.HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
                failure = ModelFailure("transport", str(exc)[:300], call_id)
                self.log.append("model_error", {"call_id": call_id, "error_kind": failure.kind,
                                                "message": str(failure), "elapsed": round(time.monotonic() - started, 3)})
                raise failure from exc
        assert last_error is not None
        raise last_error
