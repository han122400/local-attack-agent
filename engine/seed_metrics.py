"""Derive warm-start measurements from a run journal without fabricating success."""

import re

from engine.contracts import dedup_key
from engine.execution_evidence import executed_node_ids


def _observed_prompt_pin(record):
    """Validate the prompt body actually supplied to every execution model request."""
    pins = []
    for event in record["events"]:
        if event.get("kind") != "model_request":
            continue
        pin = event.get("system_prompt_sha256")
        if not isinstance(pin, str) or re.fullmatch(r"[0-9a-f]{64}", pin) is None:
            raise ValueError("Run has missing or invalid prompt hashes")
        pins.append(pin)
    if len(set(pins)) > 1:
        raise ValueError("Run has inconsistent prompt hashes")
    return pins[0] if pins else None


def _initial_seed_state(record):
    """Replay the completed seed prefix instead of comparing final run state."""
    from engine.tree import replay_tree
    events = record["events"]
    completed = [index for index, event in enumerate(events) if event.get("kind") == "seed_completed"]
    if len(completed) != 1:
        raise ValueError("Seed state completion record missing or ambiguous")
    state = replay_tree(events[:completed[0] + 1], record["metadata"]["run_id"])
    keys = ("id", "parent_id", "child_ids", "kind", "locator", "hypothesis", "dedup_key", "discovery_paths",
            "exploration_state", "investigation_state", "observations", "seed_prior_observations", "seeded")
    return [{key: node.get(key) for key in keys} for node in state["nodes"].values()]


def compare_seed_runs(first, second, *, first_attack_call_ids=None, second_attack_call_ids=None):
    """Compare recorded runs against one fixed imported baseline, not model claims."""
    records = (first, second)
    fields = ("experiment_mode", "target", "goal", "model", "model_settings", "scorer_provider",
              "scorer_model", "scorer_settings", "v4_settings", "budgets", "prompt_version")
    left, right = (record["metadata"] for record in records)
    if left.get("experiment_mode") != "v4" or any(left.get(key) != right.get(key) for key in fields):
        raise ValueError("Comparison settings differ")
    prompt_pins = tuple(_observed_prompt_pin(record) for record in records)
    if None not in prompt_pins and prompt_pins[0] != prompt_pins[1]:
        raise ValueError("Runs used different prompt bodies")
    prompt_verified = None not in prompt_pins
    seeded = [record for record in records if (record["metadata"].get("seed_settings") or {}).get("start_mode") == "seeded"]
    if not seeded:
        raise ValueError("Comparison requires a seeded run")
    source = seeded[0]["metadata"].get("seed_source") or {}
    if not source.get("run_id") or type(source.get("last_event_seq")) is not int:
        raise ValueError("Seed source identity is missing")
    baseline = {node["dedup_key"] for node in seeded[0]["state"]["nodes"].values()
                if node.get("seeded") and node.get("kind") in ("surface", "candidate")}
    initial_state = _initial_seed_state(seeded[0]) if len(seeded) > 1 else None
    for record in seeded[1:]:
        other = record["metadata"].get("seed_source") or {}
        keys = {node["dedup_key"] for node in record["state"]["nodes"].values()
                if node.get("seeded") and node.get("kind") in ("surface", "candidate")}
        if ((other.get("run_id"), other.get("last_event_seq"), keys) != (source["run_id"], source["last_event_seq"], baseline)
                or _initial_seed_state(record) != initial_state):
            raise ValueError("Seeded runs do not use the same seed state")
    results = []
    for record, reviewed, prompt_pin in zip(records, (first_attack_call_ids, second_attack_call_ids), prompt_pins):
        settings = record["metadata"].get("seed_settings") or {}
        results.append({"run_id": record["metadata"].get("run_id"), "start_mode": settings.get("start_mode", "fresh"),
                        "system_prompt_sha256": prompt_pin,
                        "reverify": settings.get("reverify") if settings.get("start_mode") == "seeded" else None,
                        "metrics": seed_metrics(record["events"], record["state"], record["metadata"],
                                                baseline_keys=baseline, attack_call_ids=reviewed)})
    return {"baseline": {"run_id": source["run_id"], "last_event_seq": source["last_event_seq"],
                         "imported_keys": len(baseline)},
            "target_initial_state_verified": False,
            "target_initial_state_note": "Matching settings do not establish equal target state; verify separately.",
            "system_prompt_sha256": prompt_pins[0] if prompt_verified else None,
            "prompt_equivalence_verified": prompt_verified,
            "prompt_equivalence_note": "Observed execution prompt bodies match." if prompt_verified else
                                       "At least one run had no model requests; prompt equivalence is unverified.",
            "runs": results,
            **({"fresh": results[0]["metrics"], "seeded": results[1]["metrics"]}
               if results[0]["start_mode"] == "fresh" and results[1]["start_mode"] == "seeded" else {})}


def seed_metrics(events, state, metadata, *, baseline_keys=None, attack_call_ids=None):
    """Summarize recorded candidate tests and reuse, using None for unknowns.

    Model calls include both execution reservations and scorer requests. A test
    begins only when a candidate attempt has a corresponding execution start.
    Wall time is measured from the run's started_at, not the lab-reset end.
    """
    nodes = state.get("nodes", {})
    attempts = state.get("attempts", {})
    candidate_calls = {attempt.get("call_id") for attempt in attempts.values()
                       if nodes.get(attempt.get("node_id"), {}).get("kind") == "candidate"}
    execution_calls = {event.get("call_id") for event in events if event.get("kind") == "exec_started"}
    reserved_candidate_calls = {event.get("call_id") for event in events
                                if event.get("kind") == "v4_execution_reserved"
                                and event.get("call_id") in candidate_calls & execution_calls}
    total_calls = sum(event.get("kind") in ("v4_execution_reserved", "scorer_request_started") for event in events)
    reserved_calls = {event.get("call_id") for event in events if event.get("kind") == "v4_execution_reserved"}
    reviewed = None if attack_call_ids is None else set(attack_call_ids)
    if reviewed is not None and not reviewed <= (reserved_calls & execution_calls):
        raise ValueError("Attack classifications must refer to recorded executed model calls")
    first = next((event for event in events if event.get("kind") == "exec_started"
                  and event.get("call_id") in candidate_calls), None)
    first_seq = first.get("seq") if first else None
    model_calls = (sum(event.get("kind") in ("v4_execution_reserved", "scorer_request_started")
                       for event in events if event.get("seq", 0) <= first_seq)
                   if type(first_seq) is int else None)
    started, at = metadata.get("started_at"), first.get("at") if first else None
    duration = (at - started if type(at) in (int, float) and type(started) in (int, float)
                and at >= started else None)

    seeded = {node_id: node for node_id, node in nodes.items()
              if node.get("seeded") and node.get("kind") in ("surface", "candidate")}
    # Reservations and carried children are not evidence of work in this run.
    used = set(seeded) & executed_node_ids(state, events)
    coverage = {"used": len(used), "total": len(seeded),
                "ratio": len(used) / len(seeded) if seeded else None}

    seeded_mode = (metadata.get("seed_settings") or {}).get("start_mode") == "seeded"
    keys = set(baseline_keys) if baseline_keys is not None else {node.get("dedup_key") for node in seeded.values()}
    has_baseline = baseline_keys is not None or seeded_mode
    discoveries, duplicates = 0, 0
    for event in events:
        payload = event.get("payload", {})
        if event.get("kind") != "node_result_imported" or payload.get("execution_status") != "succeeded":
            continue
        for item in payload.get("result", {}).get("discovered", []):
            discoveries += 1
            if dedup_key(item) in keys:
                duplicates += 1
    duplicate_stats = {"duplicates": duplicates if has_baseline else None,
                       "discoveries": discoveries,
                       "ratio": duplicates / discoveries if has_baseline and discoveries else None}
    return {
        "time_to_first_candidate_test_seconds": duration,
        "model_calls_to_first_candidate_test": model_calls,
        "coverage_reuse": coverage,
        "redundant_rediscovery": duplicate_stats,
        "candidate_test_budget": {"model_calls": len(reserved_candidate_calls),
                                  "total_model_calls": total_calls,
                                  "model_call_ratio": len(reserved_candidate_calls) / total_calls if total_calls else None,
                                  "definition": "candidate-bound executed model calls / all execution and scorer model calls; not proven attacks"},
        # Classification is explicit operator input; it is not a success verdict.
        "budget_to_attack_ratio": len(reviewed) / total_calls if reviewed is not None and total_calls else None,
        "budget_to_attack_ratio_unit": "model_calls",
        "attack_budget_classification": "operator_reviewed_call_ids" if reviewed is not None else None,
        "budget_to_attack_ratio_unavailable_reason": None if reviewed is not None else "attack_cost_not_separately_recorded",
        "source_creation_cost_usd": (metadata.get("seed_source") or {}).get("source_creation_cost_usd"),
    }
