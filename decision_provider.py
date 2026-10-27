"""OpenRouter Decisions transport. Separate from the execution-model SSE API."""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

import httpx2 as httpx

from model_providers import openrouter_key
from risk_rubric import (ScoreContractError, build_score_request, canonical_bytes,
                         validate_response, validate_usage)


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
                from v4_tree import _json_result
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
