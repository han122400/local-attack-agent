"""Compare two finalized v4 runs against the same seeded baseline, without live execution."""
import argparse
import json

from engine.paths import RUNS_ROOT
from engine.seed_metrics import compare_seed_runs
from engine.seed_source import resolve_seed
from engine.session_log import SessionStore, validate_run_id
from engine.tree import _read_bounded


def load_record(store, run_id):
    run_id = validate_run_id(run_id)
    metadata = store.read(run_id)
    source = resolve_seed(store, run_id, metadata["target"])
    events = [json.loads(line) for line in _read_bounded(store.root, f"{run_id}/events.jsonl", 64 * 1024 * 1024).decode("utf-8").splitlines()]
    if (events[-1]["seq"] if events else 0) != source["last_event_seq"]:
        raise ValueError("Run changed while reading")
    return {"metadata": metadata, "events": events, "state": source["state"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first_run")
    parser.add_argument("second_run")
    parser.add_argument("--first-attack-call-id", action="append", default=None,
                        help="operator-reviewed attack call ID (repeatable)")
    parser.add_argument("--second-attack-call-id", action="append", default=None,
                        help="operator-reviewed attack call ID (repeatable)")
    parser.add_argument("--first-no-attack", action="store_true", help="first run was reviewed and had no attack calls")
    parser.add_argument("--second-no-attack", action="store_true", help="second run was reviewed and had no attack calls")
    args = parser.parse_args()
    if (args.first_no_attack and args.first_attack_call_id) or (args.second_no_attack and args.second_attack_call_id):
        parser.error("A run cannot have both attack call IDs and --no-attack")
    store = SessionStore(RUNS_ROOT)
    print(json.dumps(compare_seed_runs(load_record(store, args.first_run), load_record(store, args.second_run),
                                       first_attack_call_ids=[] if args.first_no_attack else args.first_attack_call_id,
                                       second_attack_call_ids=[] if args.second_no_attack else args.second_attack_call_id),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
