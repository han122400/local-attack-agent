"""OpenRouter Decisions transport. Separate from the execution-model SSE API."""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx2 as httpx

from engine.model_providers import openrouter_key
from engine.risk_rubric import (CRITERIA, ScoreContractError, build_ordinal_request, build_score_request,
                         canonical_bytes, validate_ordinal_answer, validate_response, validate_usage)


DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
MAX_RESPONSE_BYTES = 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable=False, attempted=True, usage=None):
        super().__init__(code)
        self.code, self.retryable, self.attempted, self.usage = code, retryable, attempted, usage


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_pin(pin: dict) -> dict:
    try:
        for field in ("id", "canonical_slug"):
            if not isinstance(pin[field], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", pin[field]) or "latest" in pin[field].lower():
                raise ValueError()
        if pin["output_modalities"] != ["decisions"] or not isinstance(pin["catalog_retrieved_at"], str):
            raise ValueError()
        if not isinstance(pin["pricing"], dict) or any(type(pin["pricing"][key]) not in (str, int, float) for key in ("prompt", "completion", "request")):
            raise ValueError()
        prices = {key: float(pin["pricing"][key]) for key in ("prompt", "completion", "request")}
        if any(not math.isfinite(value) or value < 0 for value in prices.values()):
            raise ValueError()
        return {"id": pin["id"], "canonical_slug": pin["canonical_slug"], "output_modalities": ["decisions"],
                "catalog_retrieved_at": pin["catalog_retrieved_at"], "pricing": prices}
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ProviderError("invalid_model_pin", attempted=False) from None


def estimated_cost(pin: dict, payload: dict) -> float:
    """Conservative local reservation, NOT an upstream hard billing guarantee.

    UTF-8 bytes plus 8192 tokens of server-template headroom; reserve 8192
    output tokens. Decisions exposes no output-token cap. Actual cost wins.
    """
    if pin.get("output_modalities") == ["ordinal_label"]:
        validate_local_pin(pin)
        return 0.0
    pricing = validate_pin(pin)["pricing"]
    return ((len(canonical_bytes(payload)) + 8192) * pricing["prompt"]
            + 8192 * pricing["completion"] + pricing["request"])


class OpenRouterDecisions:
    def __init__(self, *, api_key: str | None = None, client=None):
        self._key = openrouter_key() if api_key is None else api_key
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _json(self, method, url, *, timeout, payload=None):
        if not self._key:
            raise ProviderError("missing_api_key", attempted=False)
        try:
            with self._client.stream(method, url, headers={"Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json"}, content=canonical_bytes(payload) if payload else None,
                    timeout=timeout, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ProviderError(f"http_{response.status_code}",
                                        retryable=response.status_code == 429 or response.status_code >= 500)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProviderError("response_too_large")
                    chunks.append(chunk)
                # Use the same strict parser as local result files (duplicate
                # fields/non-finite JSON are errors), without keeping raw text.
                from engine.tree import _json_result
                return _json_result(b"".join(chunks))
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", retryable=True) from None
        except httpx.DecodingError:
            raise ProviderError("invalid_response_encoding") from None
        except httpx.RequestError:
            raise ProviderError("transport_error", retryable=True) from None
        except (ValueError, RecursionError):
            raise ProviderError("invalid_response_json") from None

    def resolve(self, model: str, *, timeout_seconds=20) -> dict:
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model) or "latest" in model.lower():
            raise ProviderError("explicit_model_required", attempted=False)
        catalog = self._json("GET", CATALOG_URL + "?output_modalities=decisions", timeout=timeout_seconds)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
            raise ProviderError("invalid_catalog", attempted=False)
        entries = [item for item in catalog["data"] if isinstance(item, dict) and item.get("id") == model]
        if len(entries) != 1:
            raise ProviderError("unsupported_decisions_model", attempted=False)
        item = entries[0]
        architecture = item.get("architecture")
        modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
        if not isinstance(modalities, list) or "decisions" not in modalities:
            raise ProviderError("unsupported_decisions_model", attempted=False)
        pricing = item.get("pricing", {})
        if not isinstance(pricing, dict):
            raise ProviderError("invalid_catalog_pricing", attempted=False)
        return validate_pin({"id": model, "canonical_slug": item.get("canonical_slug"),
                             "output_modalities": ["decisions"], "catalog_retrieved_at": utc_now(),
                             "pricing": {"prompt": pricing.get("prompt"), "completion": pricing.get("completion"),
                                         "request": pricing.get("request", "0")}})

    def list_models(self, *, timeout_seconds=20) -> list[dict]:
        catalog = self._json("GET", CATALOG_URL + "?output_modalities=decisions", timeout=timeout_seconds)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
            raise ProviderError("invalid_catalog", attempted=False)
        models = []
        for item in catalog["data"]:
            if not isinstance(item, dict):
                continue
            model = item.get("id")
            if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+", model)
                    or "latest" in model.lower()):
                continue
            architecture = item.get("architecture")
            modalities = architecture.get("output_modalities") if isinstance(architecture, dict) else None
            if not isinstance(modalities, list) or "decisions" not in modalities:
                continue
            pricing = item.get("pricing")
            if not isinstance(pricing, dict):
                continue
            try:
                pin = validate_pin({"id": model, "canonical_slug": item.get("canonical_slug"),
                    "output_modalities": ["decisions"], "catalog_retrieved_at": utc_now(),
                    "pricing": {"prompt": pricing.get("prompt"), "completion": pricing.get("completion"),
                                "request": pricing.get("request", "0")}})
            except ProviderError:
                continue
            models.append({"id": model, "name": item.get("name") or model,
                           "pricing": pin["pricing"], "output_modalities": ["decisions"]})
        return sorted(models, key=lambda item: item["id"])

    def evaluate(self, pin, parent, siblings, node_ids, *, max_input_bytes, timeout_seconds) -> dict:
        pin = validate_pin(pin)
        try:
            # Rebuild the allowlisted projection at the egress boundary, even
            # when a caller has already prepared/logged a safe request.
            payload = build_score_request(pin["id"], parent, siblings, node_ids)
            if len(canonical_bytes(payload)) > max_input_bytes:
                raise ScoreContractError("input_limit")
        except ScoreContractError as exc:
            raise ProviderError(str(exc), attempted=False) from None
        started = time.monotonic()
        response = self._json("POST", DECISIONS_URL, timeout=timeout_seconds, payload=payload)
        usage = None
        try:
            usage = validate_usage(response.get("usage")) if isinstance(response, dict) else None
            result = validate_response(response, {pin["id"], pin["canonical_slug"]}, node_ids)
        except (ScoreContractError, TypeError):
            raise ProviderError("invalid_response_contract", usage=usage) from None
        result["elapsed_seconds"] = time.monotonic() - started
        return result


def validate_local_pin(pin: dict) -> dict:
    try:
        model = pin["id"]
        if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", model)
                or pin["canonical_slug"] != model or pin["output_modalities"] != ["ordinal_label"]
                or not isinstance(pin["catalog_retrieved_at"], str)
                or not isinstance(pin["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", pin["digest"])):
            raise ValueError()
        pricing = pin["pricing"]
        if not isinstance(pricing, dict) or any(type(pricing.get(key)) not in (int, float) or pricing[key] != 0
                                                for key in ("prompt", "completion", "request")):
            raise ValueError()
        return {"id": model, "canonical_slug": model, "output_modalities": ["ordinal_label"],
                "catalog_retrieved_at": pin["catalog_retrieved_at"], "digest": pin["digest"],
                "pricing": {"prompt": 0, "completion": 0, "request": 0}}
    except (KeyError, TypeError, ValueError):
        raise ProviderError("invalid_model_pin", attempted=False) from None


def build_local_chat_payload(logical: dict, settings: dict) -> dict:
    """Single wire contract for budgeting, replay validation, and egress."""
    ids = logical["node_ids"]
    properties = {node_id: {"type": "integer", "enum": list(range(len(CRITERIA)))} for node_id in ids}
    schema = {"type": "object", "additionalProperties": False, "required": ["scores"],
        "properties": {"scores": {"type": "object", "additionalProperties": False,
                                   "required": list(ids), "properties": properties}}}
    return {"model": logical["model"], "stream": False, "think": False, "format": schema,
        "options": {"temperature": settings.get("temperature", 0),
                    "num_ctx": settings.get("num_ctx", 8192),
                    "num_predict": settings.get("num_predict", 512)},
        "messages": [{"role": "system", "content": "Return only JSON. For each requested node, choose one integer 0-4 using the ordered risk criteria. Unknown facts are not evidence. Scores are priorities, never confirmed vulnerabilities."},
                     {"role": "user", "content": canonical_bytes(logical).decode("utf-8")}]}


class LocalOllamaScorer:
    """Structured ordinal scoring over a loopback Ollama API."""

    def __init__(self, *, settings=None, client=None):
        settings = settings or {}
        base = settings.get("base_url", "http://127.0.0.1:11434/")
        parsed = urlsplit(base)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ProviderError("local_endpoint_must_be_loopback", attempted=False)
        self.base_url = base.rstrip("/")
        self.settings = settings
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._owns_client = client is None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _json(self, method, path, *, timeout, payload=None):
        try:
            with self._client.stream(method, self.base_url + path, timeout=timeout,
                    follow_redirects=False, headers={"Accept-Encoding": "identity", "Content-Type": "application/json"},
                    content=canonical_bytes(payload) if payload is not None else None) as response:
                if response.status_code != 200:
                    raise ProviderError(f"http_{response.status_code}",
                                        retryable=response.status_code == 429 or response.status_code >= 500)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProviderError("response_too_large")
                    chunks.append(chunk)
                from engine.tree import _json_result
                return _json_result(b"".join(chunks))
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("timeout", retryable=True) from None
        except httpx.DecodingError:
            raise ProviderError("invalid_response_encoding") from None
        except httpx.RequestError:
            raise ProviderError("transport_error", retryable=True) from None
        except (ValueError, RecursionError, TypeError):
            raise ProviderError("invalid_response_json") from None

    def resolve(self, model: str, *, timeout_seconds=20) -> dict:
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", model):
            raise ProviderError("explicit_model_required", attempted=False)
        tags = self._json("GET", "/api/tags", timeout=timeout_seconds)
        entries = tags.get("models") if isinstance(tags, dict) else None
        if not isinstance(entries, list):
            raise ProviderError("invalid_catalog", attempted=False)
        matches = [item for item in entries if isinstance(item, dict) and item.get("name") == model]
        if len(matches) != 1:
            raise ProviderError("unsupported_local_model", attempted=False)
        return validate_local_pin({"id": model, "canonical_slug": model,
            "output_modalities": ["ordinal_label"], "catalog_retrieved_at": utc_now(),
            "digest": matches[0].get("digest"), "pricing": {"prompt": 0, "completion": 0, "request": 0}})

    def list_models(self, *, timeout_seconds=20) -> list[dict]:
        tags = self._json("GET", "/api/tags", timeout=timeout_seconds)
        if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
            raise ProviderError("invalid_catalog", attempted=False)
        models = []
        for item in tags["models"]:
            if not isinstance(item, dict):
                continue
            model = item.get("name")
            try:
                pin = validate_local_pin({"id": model, "canonical_slug": model,
                    "output_modalities": ["ordinal_label"], "catalog_retrieved_at": utc_now(),
                    "digest": item.get("digest"), "pricing": {"prompt": 0, "completion": 0, "request": 0}})
            except ProviderError:
                continue
            models.append({"id": model, "name": model, "digest": pin["digest"],
                           "output_modalities": ["ordinal_label"], "pricing": pin["pricing"]})
        return sorted(models, key=lambda item: item["id"])

    def evaluate(self, pin, parent, siblings, node_ids, *, max_input_bytes, timeout_seconds) -> dict:
        pin = validate_local_pin(pin)
        started = time.monotonic()
        current = self.resolve(pin["id"], timeout_seconds=timeout_seconds)
        if current["digest"] != pin["digest"]:
            raise ProviderError("model_digest_changed", attempted=True)
        try:
            logical = build_ordinal_request(pin["id"], parent, siblings, node_ids)
            payload = build_local_chat_payload(logical, self.settings)
            if len(canonical_bytes(payload)) > max_input_bytes:
                raise ScoreContractError("input_limit")
        except ScoreContractError as exc:
            raise ProviderError(str(exc), attempted=False) from None
        response = self._json("POST", "/api/chat", timeout=timeout_seconds, payload=payload)
        usage = None
        if isinstance(response, dict):
            try:
                usage = validate_usage({"input_tokens": response.get("prompt_eval_count"),
                                        "output_tokens": response.get("eval_count"), "cost": 0})
            except ScoreContractError:
                pass
        try:
            if (not isinstance(response, dict) or response.get("model") != pin["id"] or response.get("done") is not True
                    or not isinstance(response.get("message"), dict)
                    or not isinstance(response["message"].get("content"), str) or usage is None):
                raise ScoreContractError("invalid_response_contract")
            from engine.tree import _json_result
            answer = _json_result(response["message"]["content"].encode("utf-8"))
            if (not isinstance(answer, dict) or set(answer) != {"scores"} or not isinstance(answer["scores"], dict)
                    or set(answer["scores"]) != set(node_ids)):
                raise ScoreContractError("invalid_answer_mapping")
            scores = {key: validate_ordinal_answer(answer["scores"][key]) for key in node_ids}
        except (ScoreContractError, ValueError, TypeError, KeyError, UnicodeError):
            raise ProviderError("invalid_response_contract", usage=usage) from None
        return {"model": pin["id"], "usage": usage, "scores": scores,
                "node_errors": {}, "diagnostics": {}, "elapsed_seconds": time.monotonic() - started}
