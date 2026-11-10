"""One synthetic Decisions contract probe, separate from target experiments."""

import argparse
import json
import time
import uuid
from pathlib import Path
from engine.paths import RUNS_ROOT

from dotenv import dotenv_values

from engine.decision_provider import OpenRouterDecisions, ProviderError, estimated_cost, utc_now
from engine.risk_rubric import (CONTRACT_VERSION, RUBRIC_VERSION, build_score_request,
                         PROBABILITY_SUM_TOLERANCE, WEIGHTED_SCORE_TOLERANCE)
from engine.session_log import atomic_write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize exactly one synthetic POST; no retries")
    parser.add_argument("--env-file", type=Path, help="Read only OPENROUTER_API_KEY, without exporting environment variables")
    parser.add_argument("--output-dir", type=Path, default=RUNS_ROOT / "scorer-contract-probes")
    args = parser.parse_args()
    parent = {"id": "N0001", "kind": "root", "locator": {"url": "https://example.test/", "method": "GET", "input": "", "auth_context": "anonymous"}}
    child = {"id": "N0002", "kind": "candidate", "locator": {"url": "https://example.test/api/search", "method": "GET", "input": "q", "auth_context": "anonymous"},
             "hypothesis": {"type": "sqli", "statement": "Synthetic fixture, no target traffic"},
             "observations": {"input_control": "full", "query_behavior": "error", "baseline_compared": True}}
    model = "typesafe/jev-1.14"
    payload = build_score_request(model, parent, [child], ["N0002"])
    if not args.live:
        print(json.dumps({"mode": "dry_run", "request": payload}, ensure_ascii=False))
        return 0
    key = None
    if args.env_file:
        key = dotenv_values(args.env_file, interpolate=False, encoding="utf-8-sig").get("OPENROUTER_API_KEY", "").strip()
    adapter = OpenRouterDecisions(api_key=key)
    proof = {"purpose": "synthetic_contract_probe", "started_at": utc_now(), "status": "preparing",
             "contract_version": CONTRACT_VERSION, "rubric_version": RUBRIC_VERSION,
             "probability_sum_tolerance": PROBABILITY_SUM_TOLERANCE, "weighted_score_tolerance": WEIGHTED_SCORE_TOLERANCE,
             "max_posts": 1, "timeout_seconds": 20, "max_input_bytes": 16384, "max_cost_usd": 0.001,
             "request_count": 0, "request": payload}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / (uuid.uuid4().hex + ".json")
    atomic_write_json(path, proof)
    started = time.monotonic()
    try:
        pin = adapter.resolve(model)
        proof.update(model_pin=pin, estimated_cost_usd=estimated_cost(pin, payload))
        if proof["estimated_cost_usd"] > proof["max_cost_usd"]:
            raise ProviderError("cost_reservation_exceeded", attempted=False)
        proof.update(status="request_started", request_count=1)
        atomic_write_json(path, proof)
        result = adapter.evaluate(pin, parent, [child], ["N0002"], max_input_bytes=16384, timeout_seconds=20)
        proof["response"] = result
        proof["status"] = "passed" if not result["node_errors"] and result["usage"]["cost"] <= proof["max_cost_usd"] else "failed"
    except ProviderError as exc:
        proof.update(status="failed", error=exc.code, usage=exc.usage)
    finally:
        adapter.close()
        proof.update(finished_at=utc_now(), elapsed_seconds=time.monotonic() - started)
        atomic_write_json(path, proof)
    print(json.dumps({"status": proof["status"], "request_count": proof["request_count"], "proof": str(path),
                      "model_pin": proof.get("model_pin"), "response": proof.get("response"), "error": proof.get("error")}, ensure_ascii=False))
    return 0 if proof["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
