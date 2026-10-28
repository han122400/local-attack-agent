"""Run one recorded v3/v4 pair against the fixed, isolated Juice Shop lab.

This is an explicit live experiment: it restarts juice-lab, launches the agent's
headless browser, and sends v4 scoring requests to OpenRouter. It is never run
by the test suite or on import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

from dotenv import dotenv_values

from command_runtime import installed_chromium
from decision_provider import OpenRouterDecisions
from experiment_config import validate_experiment_config
from explorer import ExplorerV3, RUNS_ROOT, validate_budgets
from lab import reset_lab
from model_settings import validate_model_settings
from planning import load_prompt
from scorer_state import effective_settings
from session_log import atomic_write_json
from v4_oracle import lab_identity
from v4_policy import CHALLENGE_KEY, LAB_TARGET, execution_settings


PROTOCOL = "a5-paired-v1"
CONFIG_PATH = Path(__file__).resolve().parent / "benchmarks" / "a5-pair-v1.json"
PAIRS_DIR = RUNS_ROOT / "a5_pairs"


def validate_config(raw: dict) -> dict:
    """Pin one comparable pair; v3 cannot enforce a remote execution cost cap."""
    if not isinstance(raw, dict) or raw.get("protocol_version") != PROTOCOL or raw.get("target") != LAB_TARGET:
        raise ValueError("Unknown A5 protocol or target")
    if raw.get("expected_challenge") != CHALLENGE_KEY:
        raise ValueError("Unknown A5 ground-truth challenge")
    model = raw.get("execution_model")
    if not isinstance(model, str) or not model or model.startswith("openrouter:"):
        raise ValueError("A5 v1 requires the same local Ollama execution model in both modes")
    if not isinstance(raw.get("goal"), str) or not raw["goal"].strip():
        raise ValueError("A5 goal is required")
    validate_model_settings(raw.get("model_settings"))
    validate_budgets(raw.get("budgets"))
    selected = validate_experiment_config({"experiment_mode": "v4", "scorer_provider": raw.get("scorer_provider"),
        "scorer_model": raw.get("scorer_model"), "scorer_settings": raw.get("scorer_settings")})
    if selected["scorer_provider"] != "openrouter":
        raise ValueError("A5 v1 requires the pinned OpenRouter scorer")
    effective_settings(selected)
    execution = execution_settings(raw.get("v4_settings"))
    if execution["exclusive_lab"] is not True:
        raise ValueError("A5 v1 confirmation requires an isolated lab assertion")
    return raw


def config_digest(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def check_prompt_pins(manifest: dict, current: dict[str, str]) -> dict[str, str]:
    if manifest.get("prompt_pins") not in (None, current):
        raise ValueError("A5 prompt changed after pair creation")
    for mode, entry in manifest.get("runs", {}).items():
        recorded = (entry.get("metrics") or {}).get("prompt_sha256")
        if recorded is not None and recorded != current.get(mode):
            raise ValueError("A5 prompt changed after first mode")
    return current


def challenge_snapshot() -> dict:
    """Read only the one fixed challenge; never retain the full challenge list."""
    with urlopen(LAB_TARGET + "api/Challenges/", timeout=5) as response:
        payload = json.load(response)
    rows = payload.get("data") if isinstance(payload, dict) and payload.get("status") == "success" else None
    matches = [row for row in rows if isinstance(row, dict) and row.get("key") == CHALLENGE_KEY] if isinstance(rows, list) else []
    if len(matches) != 1 or type(matches[0].get("solved")) is not bool:
        raise RuntimeError("Fixed challenge state is unavailable")
    return {key: matches[0].get(key) for key in ("id", "key", "name", "category", "solved")}


def _events(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def final_observation(entry: dict) -> dict | None:
    """Compare with the baseline after any controller-owned initial reset."""
    observed = next((item for item in reversed(entry.get("reset_observations", []))
                     if item.get("phase") == "resetting"), None)
    if observed is None:
        return None
    effective = entry.get("effective_baseline_identity") or entry.get("baseline_identity")
    return {**observed, "baseline_identity": effective}


def summarize_run(run_dir: Path, mode: str, final_observation: dict | None,
                  *, expected_prompt_sha256: str | None = None) -> dict:
    """Compute factual metrics. Model claims are never promoted to confirmed."""
    meta = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    events = _events(run_dir / "events.jsonl")
    if meta.get("experiment_mode") != mode or meta.get("prompt_version") != mode:
        raise ValueError("Run mode/prompt mismatch")
    requests = [event for event in events if event["kind"] == "model_request"]
    responses = [event for event in events if event["kind"] == "model_response"]
    executions = [event for event in events if event["kind"] == "exec_finished"]
    response_ids = {event["call_id"] for event in responses}
    complete_tokens = len(responses) == len(requests) and all(
        type(event.get("prompt_eval_count")) is int and type(event.get("eval_count")) is int for event in responses)
    observation = final_observation or {}
    challenge = observation.get("challenge") or {}
    identity = observation.get("identity") or {}
    baseline_identity = observation.get("baseline_identity") or {}
    same_identity = (identity == baseline_identity) if identity and baseline_identity else None
    prompt_hashes = {event.get("system_prompt_sha256") for event in requests}
    if (len(prompt_hashes) > 1 or (prompt_hashes and
            (not isinstance(next(iter(prompt_hashes)), str) or len(next(iter(prompt_hashes))) != 64))):
        raise ValueError("Run has inconsistent prompt hashes")
    prompt_hash = next(iter(prompt_hashes)) if prompt_hashes else expected_prompt_sha256
    if expected_prompt_sha256 is not None and prompt_hash != expected_prompt_sha256:
        raise ValueError("Run used a different prompt body")
    result = {"run_id": meta["run_id"], "mode": mode, "phase": meta.get("phase"),
        "run_result": meta.get("run_result"), "finish_reason": meta.get("finish_reason"),
        "prompt_sha256": prompt_hash, "model_calls": len(requests),
        "model_responses": len(responses), "unanswered_model_calls": len({event["call_id"] for event in requests} - response_ids),
        "tool_calls": sum(event["kind"] == "exec_started" for event in events),
        "failed_tool_calls": sum(event.get("exit_code") != 0 or event.get("timed_out") is True
                                 for event in executions),
        "execution_input_tokens": sum(event["prompt_eval_count"] for event in responses) if complete_tokens else None,
        "execution_output_tokens": sum(event["eval_count"] for event in responses) if complete_tokens else None,
        "execution_api_cost_usd": 0.0, "total_api_cost_usd": 0.0,
        "challenge_solved_before_cleanup": challenge.get("solved"),
        "challenge_observation_error": observation.get("error"), "same_lab_identity": same_identity,
        "wall_seconds_including_resets": round(meta["finished_at"] - meta["started_at"], 3)
            if isinstance(meta.get("finished_at"), (int, float)) and isinstance(meta.get("started_at"), (int, float)) else None,
        "claim_adjudication": "manual_required"}
    if mode == "v3":
        findings = meta.get("findings") or []
        started = requests[0].get("at") if requests else None
        finalized = next((event.get("at") for event in events if event["kind"] == "finalize_started"), None)
        result.update(confirmed_by_harness=None, model_report_present=bool(meta.get("report")),
                      reported_evidence_observations=sum(bool(item.get("evidence")) for item in findings),
                      repeated_observation_digests=sum(item.get("repeat_of") is not None for item in findings),
                      total_model_calls=len(requests), scorer_calls=0,
                      exploration_elapsed_seconds=round(finalized - started, 3)
                          if isinstance(started, (int, float)) and isinstance(finalized, (int, float)) else None,
                      final_model_summary=next((event.get("reason") for event in reversed(events)
                                                if event["kind"] == "run_finished_by_model"), None))
    else:
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        stats = report.get("stats") or {}
        usage = report.get("usage") or {}
        node_ids = json.loads((run_dir / "tree.json").read_text(encoding="utf-8")).get("node_ids") or []
        discovery_submissions = sum(event["kind"] == "node_discovered" for event in events)
        discovery_submissions += sum(len((event.get("payload") or {}).get("result", {}).get("discovered", []))
                                     for event in events if event["kind"] == "node_result_imported")
        result.update(report_status=report.get("status"), confirmed_by_harness=stats.get("confirmed"),
                      candidates=stats.get("candidates"), tested_candidates=stats.get("tested_candidates"),
                      inconclusive=stats.get("inconclusive"), unscored=stats.get("unscored"),
                      scorer_calls=usage.get("scoring_calls"), scorer_input_tokens=usage.get("scoring_input_tokens"),
                      scorer_output_tokens=usage.get("scoring_output_tokens"),
                      total_model_calls=usage.get("model_calls"),
                      exploration_elapsed_seconds=usage.get("elapsed_seconds"),
                      total_api_cost_usd=usage.get("cost_usd"), unknown_cost_requests=usage.get("unknown_cost_requests"),
                      repeated_discoveries=discovery_submissions - max(0, len(node_ids) - 1),
                      known_case_false_positive=(stats.get("confirmed", 0) > 0 and challenge.get("solved") is False)
                          if stats.get("confirmed") is not None and challenge.get("solved") is not None else None,
                      known_case_unconfirmed_solve=(stats.get("confirmed") == 0 and challenge.get("solved") is True)
                          if stats.get("confirmed") is not None and challenge.get("solved") is not None else None)
    return result


def _ollama_digest(model: str) -> str:
    with urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
        payload = json.load(response)
    matches = [item for item in payload.get("models", []) if item.get("name") == model]
    if len(matches) != 1 or not isinstance(matches[0].get("digest"), str):
        raise RuntimeError("Pinned Ollama model is unavailable")
    return matches[0]["digest"]


def run_pair(config: dict, *, env_file: Path, resume_path: Path | None = None) -> Path:
    if not installed_chromium():
        raise RuntimeError("Headless Chromium is required for the explicitly requested A5 runs")
    if not env_file.is_file():
        raise RuntimeError("OpenRouter key file is unavailable")
    key = dotenv_values(env_file, interpolate=False, encoding="utf-8-sig").get("OPENROUTER_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("OpenRouter key is unavailable")
    adapter = OpenRouterDecisions(api_key=key.strip())
    key = None  # The key is held only in this parent-process adapter, never in subprocess environment.
    PAIRS_DIR.mkdir(parents=True, exist_ok=True)
    if resume_path is None:
        pair_id = uuid.uuid4().hex
        output = PAIRS_DIR / f"{pair_id}.json"
        manifest = {"protocol_version": PROTOCOL, "pair_id": pair_id, "status": "preflight",
                    "configuration": config, "configuration_sha256": config_digest(config),
                    "started_at": time.time(), "order": ["v3", "v4"], "runs": {}}
    else:
        output = resume_path.resolve()
        if output.parent != PAIRS_DIR.resolve() or len(output.stem) != 32 or output.suffix != ".json":
            raise ValueError("Resume file must be an A5 pair manifest")
        manifest = json.loads(output.read_text(encoding="utf-8"))
        if (manifest.get("protocol_version") != PROTOCOL or manifest.get("pair_id") != output.stem
                or manifest.get("configuration_sha256") != config_digest(config)
                or manifest.get("configuration") != config or manifest.get("order") != ["v3", "v4"]):
            raise ValueError("Resume configuration differs from the recorded pair")
        manifest["status"] = "preflight"
        manifest.pop("error", None)
    lock = threading.RLock()

    def save():
        with lock:
            atomic_write_json(output, manifest)

    save()
    try:
        manifest["prompt_pins"] = check_prompt_pins(manifest,
            {mode: load_prompt(mode)[1] for mode in ("v3", "v4")})
        save()
        digest = _ollama_digest(config["execution_model"])
        pin = adapter.resolve(config["scorer_model"])
        if manifest.get("ollama_digest") not in (None, digest):
            raise RuntimeError("Execution model digest changed during the pair")
        if manifest.get("scorer_pin") and (manifest["scorer_pin"]["id"], manifest["scorer_pin"]["canonical_slug"]) != (pin["id"], pin["canonical_slug"]):
            raise RuntimeError("Scorer model identity changed during the pair")
        manifest["ollama_digest"] = digest
        manifest["scorer_pin"] = pin
        manifest.setdefault("lab_identity_before_pair", lab_identity())
        save()
        for mode in ("v3", "v4"):
            prior = manifest["runs"].get(mode)
            if prior is not None:
                metrics = prior.get("metrics") or {}
                if (prior.get("status") != "finished" or metrics.get("phase") != "finished"
                        or metrics.get("same_lab_identity") is not True
                        or metrics.get("challenge_solved_before_cleanup") is None):
                    raise RuntimeError(f"A5 {mode} run must be recovered or adjudicated before resume")
                print(f"A5 {mode}: reuse completed run {prior['run_id']}", flush=True)
                continue
            print(f"A5 {mode}: verified reset and start", flush=True)
            reset = reset_lab()
            if reset.get("verified") is not True:
                raise RuntimeError("A5 initial lab reset was not verified")
            baseline_challenge = challenge_snapshot()
            baseline_identity = lab_identity()
            if baseline_challenge["solved"] is not False:
                raise RuntimeError("A5 challenge is already solved after reset")
            entry = {"status": "starting", "baseline_reset": reset,
                     "baseline_challenge": baseline_challenge, "baseline_identity": baseline_identity,
                     "reset_observations": []}
            manifest["runs"][mode] = entry
            save()
            explorer = None

            def tracked_reset(*, deadline=None):
                nonlocal baseline_identity
                run_id = explorer.active_run_id if explorer else None
                try:
                    phase = explorer.store.read(run_id).get("phase") if run_id else "unknown"
                except (KeyError, ValueError, OSError):
                    phase = "unknown"
                observed = {"phase": phase, "at": time.time(), "baseline_identity": baseline_identity}
                try:
                    observed["challenge"] = challenge_snapshot()
                    observed["identity"] = lab_identity()
                except Exception as exc:
                    observed["error"] = f"{type(exc).__name__}: {exc}"[:200]
                with lock:
                    entry["reset_observations"].append(observed)
                    save()
                probe = reset_lab(deadline=deadline) if deadline is not None else reset_lab()
                with lock:
                    observed["reset_probe"] = probe
                    if phase == "preparing" and probe.get("verified") is True:
                        baseline_identity = lab_identity()
                        entry["effective_baseline_identity"] = baseline_identity
                        entry["effective_baseline_challenge"] = challenge_snapshot()
                        if entry["effective_baseline_challenge"]["solved"] is not False:
                            raise RuntimeError("Challenge was not reset to unsolved")
                    save()
                return probe

            explorer = ExplorerV3(runs_root=RUNS_ROOT, lab_reset=tracked_reset, scorer_adapter=adapter)
            kwargs = {"goal": config["goal"], "model_settings": config["model_settings"],
                      "budgets": config["budgets"], "prompt_version": mode, "experiment_mode": mode}
            if mode == "v4":
                kwargs.update(scorer_provider=config["scorer_provider"], scorer_model=config["scorer_model"],
                              scorer_settings=config["scorer_settings"], v4_settings=config["v4_settings"])
            run_id = explorer.start(config["target"], config["execution_model"], **kwargs)
            if not run_id:
                raise RuntimeError("A5 controller did not start")
            entry.update(run_id=run_id, status="running", started_at=time.time())
            save()
            try:
                while explorer.worker and explorer.worker.is_alive():
                    explorer.worker.join(timeout=20)
                    if explorer.worker.is_alive():
                        elapsed = int(time.time() - entry["started_at"])
                        print(f"A5 {mode}: {elapsed}s elapsed", flush=True)
            except KeyboardInterrupt:
                explorer.stop(run_id)
                if explorer.worker:
                    explorer.worker.join(timeout=config["budgets"]["finalize_seconds"] + 30)
                raise
            final = final_observation(entry)
            entry["metrics"] = summarize_run(RUNS_ROOT / run_id, mode, final,
                                               expected_prompt_sha256=manifest["prompt_pins"][mode])
            entry["status"] = "finished" if entry["metrics"]["phase"] == "finished" else "failed"
            entry["finished_at"] = time.time()
            save()
            if entry["status"] != "finished":
                raise RuntimeError(f"A5 {mode} run requires cleanup/recovery before the pair can continue")
            if (entry["metrics"]["challenge_solved_before_cleanup"] is None
                    or entry["metrics"]["same_lab_identity"] is not True
                    or (mode == "v4" and entry["metrics"]["report_status"] != "complete")):
                raise RuntimeError(f"A5 {mode} run lacks comparable ground truth or a complete report")
        manifest["status"] = "completed"
        manifest["finished_at"] = time.time()
        save()
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"[:300]
        manifest["finished_at"] = time.time()
        save()
        raise
    finally:
        adapter.close()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually run the two live local-lab experiments")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Versioned A5 configuration file")
    parser.add_argument("--exclusive-lab", action="store_true", help="Assert nobody else is using the fixed lab")
    parser.add_argument("--env-file", type=Path, help="Read the existing OpenRouter key without exporting it to commands")
    parser.add_argument("--resume", type=Path, help="Continue a preserved pair after its earlier run was adjudicated")
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
    if not args.execute:
        print(json.dumps({"mode": "dry_run", "protocol": PROTOCOL, "configuration_sha256": config_digest(config),
                          "target": config["target"], "model": config["execution_model"], "order": ["v3", "v4"]}))
        return 0
    if not args.exclusive_lab or args.env_file is None:
        parser.error("Live A5 requires --exclusive-lab and --env-file")
    path = run_pair(config, env_file=args.env_file, resume_path=args.resume)
    print(json.dumps({"status": "completed", "pair_manifest": str(path)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
