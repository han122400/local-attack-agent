"""Read a finalized source run without opening or changing its journal."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from engine.contracts import SCHEMA_VERSION, normalize_url
from engine.session_log import LogFailure, validate_run_id
from engine.tree import replay_tree, _read_bounded
from engine.usage import summarize_usage


def _origin(target):
    parsed = urlsplit(normalize_url(target))
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def seed_import_status(metadata, events):
    """Classify the seed transaction from the journal, never lifecycle labels.

    Fresh runs before warm-start support need no seed markers. A seeded run
    needs exactly one ordered start/completion pair and no failure marker.
    """
    starts = [event for event in events if event.get("kind") == "seed_started"]
    completions = [event for event in events if event.get("kind") == "seed_completed"]
    failures = [event for event in events if event.get("kind") == "seed_failed"]
    requested = (metadata.get("seed_settings") or {}).get("start_mode") == "seeded"
    if not requested and not starts and not completions and not failures:
        return "fresh"
    if failures:
        return "failed"
    if len(starts) == len(completions) == 1 and starts[0]["seq"] < completions[0]["seq"]:
        return "complete"
    return "incomplete"


def resolve_seed(store, selection, target):
    """Resolve one frozen source and replay its complete journal in memory."""
    expected = _origin(target)
    if selection == "latest":
        candidates = store.list_runs()
    else:
        validate_run_id(selection)
        try:
            candidates = [store.read(selection)]
        except KeyError as exc:
            raise ValueError("Seed source does not exist") from exc
    for metadata in candidates:
        try:
            eligible = (metadata.get("experiment_mode") == "v4"
                and metadata.get("phase") == "finished"
                and metadata.get("log_status") == "finalized"
                and metadata.get("cleanup_status") == "succeeded"
                and metadata.get("lab_reset_status") in ("succeeded", "not_applicable")
                and metadata.get("finish_reason") != "seed_failed"
                and metadata.get("tree_schema_version") == SCHEMA_VERSION
                and _origin(metadata.get("target")) == expected
                and normalize_url(metadata.get("target")) == normalize_url(target))
        except (TypeError, ValueError):
            eligible = False
        if not eligible:
            if selection != "latest":
                raise ValueError("Seed source is not a finalized compatible run for this target")
            continue
        run_id = validate_run_id(metadata["run_id"])
        try:
            # Bounded read and link checks keep untrusted run files inside storage.
            data = _read_bounded(store.root, f"{run_id}/events.jsonl", 64 * 1024 * 1024)
            events = [json.loads(line) for line in data.decode("utf-8").splitlines()]
            state = replay_tree(events, run_id)
            if (state["root_id"] is None
                    or normalize_url(state["nodes"][state["root_id"]]["locator"]["url"]) != normalize_url(target)):
                raise ValueError("Seed root does not match target")
            source_usage = summarize_usage(events, state)
        except (OSError, ValueError, KeyError, TypeError, LogFailure) as exc:
            if selection == "latest":
                continue
            raise ValueError("Seed source journal is invalid") from exc
        if seed_import_status(metadata, events) not in ("fresh", "complete"):
            if selection == "latest":
                continue
            raise ValueError("Source seed import is incomplete or failed")
        return {"run_id": run_id, "last_event_seq": state["last_event_seq"],
                "schema_version": SCHEMA_VERSION, "state": state,
                "source_creation_cost_usd": source_usage["cost_usd"],
                "source_unknown_cost_requests": source_usage["unknown_cost_requests"],
                "source_model_calls": source_usage["model_calls"]}
    raise ValueError("No finalized compatible seed source for this target")
