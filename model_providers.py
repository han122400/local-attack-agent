"""OpenRouter catalog and SSE adapter for the harness's model transport contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

import httpx2 as httpx
from dotenv import dotenv_values


ENV_PATH = Path(__file__).resolve().parent / ".env"
OPENROUTER_PREFIX = "openrouter:"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
_catalog_cache: dict = {}


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        # Transient failures (incomplete stream, 429, 5xx, dropped connection) may clear on a retry;
        # auth/billing/permission failures will not, so they set retryable=False.
        self.retryable = retryable


def is_openrouter(model: str) -> bool:
    return model.startswith(OPENROUTER_PREFIX)


def openrouter_key() -> str:
    # Do not load .env into os.environ: model-authored subprocesses inherit that environment.
    value = os.environ.get("OPENROUTER_API_KEY")
    if value is None:
        value = dotenv_values(ENV_PATH, interpolate=False, encoding="utf-8-sig").get("OPENROUTER_API_KEY")
    return (value or "").strip()


def _require_key() -> str:
    key = openrouter_key()
    if not key:
        raise OpenRouterError("프로젝트 .env에 OPENROUTER_API_KEY를 설정한 뒤 모델 목록을 새로고침하세요.")
    return key


def _http_failure(status: int) -> OpenRouterError:
    hint = {401: "OPENROUTER_API_KEY를 확인하세요.", 402: "크레딧 잔액 또는 사용 한도를 확인하세요.",
            403: "키의 권한과 모델 접근 설정을 확인하세요.",
            429: "호출 한도에 도달했습니다. 잠시 후 다시 시도하세요."}.get(status, "잠시 후 다시 시도하세요.")
    # Never include upstream bodies/headers: they may echo credentials or request data.
    return OpenRouterError(f"OpenRouter HTTP {status}: {hint}", retryable=status not in {401, 402, 403})


def get_openrouter_models(*, refresh: bool = False) -> list[dict]:
    key = _require_key()
    cache_key = (str(ENV_PATH), hashlib.sha256(key.encode()).hexdigest())
    cached = _catalog_cache.get(cache_key)
    if not refresh and cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(f"{OPENROUTER_URL}/models", headers={"Authorization": f"Bearer {key}"})
            if not response.is_success:
                raise _http_failure(response.status_code)
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Invalid model catalog")
        models = []
        seen = set()
        for item in payload["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise ValueError("Invalid model ID")
            model_id = item["id"]
            architecture = item.get("architecture") or {}
            if "text" not in architecture.get("output_modalities", ["text"]) or model_id in seen:
                continue
            seen.add(model_id)
            models.append({"name": OPENROUTER_PREFIX + model_id, "id": model_id,
                           "label": item.get("name") or model_id, "provider": "openrouter",
                           "context_length": item.get("context_length"),
                           "max_completion_tokens": (item.get("top_provider") or {}).get("max_completion_tokens"),
                           "supported_parameters": item.get("supported_parameters") or [],
                           "pricing": item.get("pricing") or {}})
        _catalog_cache.clear()
        _catalog_cache[cache_key] = (time.monotonic(), models)
        return models
    except OpenRouterError:
        raise
    except (httpx.HTTPError, OSError):
        raise OpenRouterError("OpenRouter 연결 실패: 네트워크 연결을 확인하세요.") from None
    except (ValueError, TypeError, AttributeError):
        raise OpenRouterError("OpenRouter 모델 목록 응답 형식이 올바르지 않습니다.") from None


def openrouter_model(model: str) -> dict:
    for item in get_openrouter_models():
        if item["name"] == model:
            return item
    raise ValueError("선택한 OpenRouter 모델을 찾지 못했습니다. 모델 목록을 새로고침하세요.")


# Tool-call serialization delimiters that some OpenRouter models (observed: z-ai/glm-flash) leak into
# the text content channel, landing inside a field value and breaking run_python as a syntax error.
_TOOL_CALL_FRAMING = ("<tool_call>", "</tool_call>", "<arg_value>", "</arg_value>",
                      "<arg_key>", "</arg_key>", "<tool_response>", "</tool_response>")


def strip_tool_call_framing(command: dict) -> tuple[dict, list[str]]:
    """Cut leaked tool-call framing out of a decoded command's string fields; return (clean, changed).

    These models emit the whole command as text in the content channel and interleave their tool-call
    delimiters into it. Because the real value always precedes the leaked framing, each string field is
    truncated at the first delimiter, and the stray scaffold punctuation left before it (e.g. a trailing
    '}) is trimmed. The delimiters are distinctive XML-ish tokens absent from legitimate Python/shell and
    from injection payloads (an XSS payload carries <svg>/<script>, never <tool_call>), and a field is
    only touched when it actually contains one — so clean commands are never altered. Deterministic.
    """
    cleaned = dict(command)
    changed: list[str] = []
    for field in ("plan", "reason", "path", "content", "command", "code"):
        value = command.get(field)
        if not isinstance(value, str) or not value:
            continue
        cut = min((value.find(token) for token in _TOOL_CALL_FRAMING if token in value), default=-1)
        if cut >= 0:
            cleaned[field] = value[:cut].rstrip("'\"{}:` \t\r\n")
            changed.append(field)
    return cleaned, changed


def _request_payload(payload: dict, info: dict) -> dict:
    supported = set(info["supported_parameters"])
    options = payload.get("options") or {}
    request = {"model": info["id"], "messages": payload["messages"], "stream": True}
    for source, destination in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"),
                                ("min_p", "min_p"), ("repeat_penalty", "repetition_penalty"),
                                ("presence_penalty", "presence_penalty"), ("frequency_penalty", "frequency_penalty"),
                                ("seed", "seed")):
        if source in options and destination in supported:
            request[destination] = options[source]
    output_limit = options.get("num_predict", -1)
    if output_limit <= 0:
        output_limit = info.get("max_completion_tokens") or 4096
    if info.get("max_completion_tokens"):
        output_limit = min(output_limit, info["max_completion_tokens"])
    if info.get("context_length"):
        # Match the explorer's character/token proxy, with room for message framing.
        # Tokenizers differ; this is a conservative estimate, not an exact token count.
        prompt_tokens = sum(math.ceil(len(message.get("content", "")) / 3.3) + 16
                            for message in payload["messages"]) + 256
        remaining = info["context_length"] - prompt_tokens
        if remaining < 128:
            raise OpenRouterError("OpenRouter 모델의 컨텍스트 공간이 부족합니다. 입력을 줄이거나 문맥이 더 큰 모델을 선택하세요.")
        output_limit = min(output_limit, remaining)
    request["max_tokens"] = output_limit
    think = payload.get("think")
    if think is not None and "reasoning" in supported:
        request["reasoning"] = {"enabled": think} if type(think) is bool else {"effort": think}
    # Prefer the cheapest provider for this model. The same model is priced very differently per
    # provider, and input tokens (re-sent context each step) dominate cost on multi-step runs.
    request["provider"] = {"sort": "price"}
    if "structured_outputs" in supported:
        # Use the portable object schema; action-specific invariants remain enforced by validate_command.
        schema = {key: value for key, value in payload["format"].items() if key != "oneOf"}
        # Some strict providers do not support minLength, so validate those constraints locally too.
        schema = {**schema, "properties": {name: {k: v for k, v in prop.items() if k != "minLength"}
                                          for name, prop in schema["properties"].items()}}
        request["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "investigation_command", "strict": True, "schema": schema}}
        # Only route to providers that honour every parameter we send (schema, reasoning, sampling).
        request["provider"]["require_parameters"] = True
    elif "response_format" in supported:
        request["response_format"] = {"type": "json_object"}
    return request


def _sse_events(lines):
    data = []
    for line in lines:
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


def openrouter_stream(payload: dict):
    key = _require_key()
    try:
        info = openrouter_model(payload["model"])
        request = _request_payload(payload, info)
        timeout = httpx.Timeout(connect=10, read=120, write=15, pool=5)
        completion = {}
        finish_reason = None
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream("POST", f"{OPENROUTER_URL}/chat/completions", json=request,
                               headers={"Authorization": f"Bearer {key}"}) as response:
                if not response.is_success:
                    raise _http_failure(response.status_code)
                for data in _sse_events(response.iter_lines()):
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        code = (chunk["error"] or {}).get("code", "unknown")
                        code = str(code) if type(code) is int else "unknown"
                        raise OpenRouterError(f"OpenRouter 스트리밍 오류 ({code}). 모델 접근 권한과 크레딧을 확인하세요.")
                    usage = chunk.get("usage") or {}
                    for source, destination in (("prompt_tokens", "prompt_eval_count"), ("completion_tokens", "eval_count")):
                        if usage.get(source) is not None:
                            completion[destination] = usage[source]
                    for choice in chunk.get("choices") or []:
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta") or {}
                        message = {"content": delta.get("content") or "",
                                   "thinking": delta.get("reasoning") or delta.get("reasoning_content") or ""}
                        if any(message.values()):
                            yield {"message": message}
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        if finish_reason not in {"stop", "length"}:
            raise OpenRouterError("OpenRouter 응답이 정상 종료되지 않았습니다. 불완전한 명령은 실행하지 않습니다.")
        yield {"done": True, "done_reason": finish_reason, **completion}
    except OpenRouterError:
        raise
    except (httpx.HTTPError, OSError):
        raise OpenRouterError("OpenRouter 연결 또는 응답 수신에 실패했습니다.") from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise OpenRouterError("OpenRouter 응답 형식이 올바르지 않습니다.") from None
