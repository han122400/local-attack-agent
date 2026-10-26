"""Controlled decision replay, NOT a website/vulnerability benchmark.

Only read_file/finish are interpreted against in-memory fixtures. Generated code is never executed.
The production scheduler, HTTP handling and prompts are not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


CASES = {
    "four_step_setup": [("A", 4, True)],
    "two_cases": [("A", 1, True), ("B", 1, True)],
    "blocked_then_easy": [("A", 1, False), ("B", 1, True)],
}
POLICIES = ("original", "stall3", "case_finish", "combined")


def profile_events(log):
    responses, starts, first_content, commands = [], {}, {}, {}
    thinking_chars, content_chars, output_limits, cursor = 0, 0, 0, 0
    while events := log.read_events(after_seq=cursor):
        for event in events:
            call = event.get("call_id")
            if event["kind"] == "model_request":
                starts[call] = event["at"]
            elif event["kind"] == "model_delta":
                if event.get("channel") == "thinking":
                    thinking_chars += len(event.get("text", ""))
                elif event.get("channel") == "content":
                    content_chars += len(event.get("text", ""))
                    if event.get("text") and call in starts:
                        first_content.setdefault(call, max(0, event["at"] - starts[call]))
            elif event["kind"] == "command_decoded" and call in starts:
                commands[call] = max(0, event["at"] - starts[call])
            elif event["kind"] == "model_error" and event.get("error_kind") == "output_limit":
                output_limits += 1
            elif event["kind"] == "model_response":
                responses.append(event)
        cursor = events[-1]["seq"]
    timed = [r for r in responses if r.get("eval_duration", 0) > 0 and r.get("eval_count") is not None]
    generation_seconds = sum(r["eval_duration"] for r in timed) / 1e9
    def total(field, scale=1):
        if not starts or len(responses) != len(starts) or any(not isinstance(r.get(field), (int, float)) for r in responses):
            return None
        value = sum(r[field] for r in responses)
        return value if scale == 1 else value / scale
    return {"valid_commands": len(commands), "valid_command_rate": len(commands) / len(starts) if starts else None,
            "mean_time_to_content_seconds": sum(first_content.values()) / len(first_content) if first_content else None,
            "mean_command_seconds": sum(commands.values()) / len(commands) if commands else None,
            "thinking_chars": thinking_chars, "content_chars": content_chars, "output_limit_count": output_limits,
            "generated_tokens": total("eval_count"),
            "completed_generation_tps": sum(r["eval_count"] for r in timed) / generation_seconds if generation_seconds else None,
            "load_seconds": total("load_duration", 1e9),
            "prompt_tokens": total("prompt_eval_count"),
            "prompt_eval_seconds": total("prompt_eval_duration", 1e9),
            "raw_outputs": [r.get("output", "") for r in responses]}


def evaluate(case, policy, decide, *, max_calls=8):
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    states = [{"id": name, "required_reads": count, "solvable": solvable,
               "reads": set(), "closed": False, "complete": False} for name, count, solvable in case]
    current, idle, finished, failure = 0, 0, False, ""
    history, trace = [], []
    started = time.monotonic()

    def task_view(state):
        return {"id": state["id"], "complete": state["complete"],
                "next_file": f"work/{state['id']}/step{min(len(state['reads']) + 1, state['required_reads'])}.json"}

    for _ in range(max_calls):
        context = {"goal": "Inspect every listed case by following fixture references; report their final facts and any blocked cases.",
                   "target": "offline fixtures only", "runtime_path": "/work",
                   "tasks": [task_view(s) for s in states if not s["closed"]],
                   "current": task_view(states[current]), "history": history[-8:], "recent": history[-2:],
                   "remaining": {"model_calls": max_calls - len(trace), "finish_soon": False}}
        try:
            command = decide(context)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {str(exc)[:200]}"
            break
        trace.append({"current": states[current]["id"], "action": command.action, "path": command.path})
        if command.action == "finish":
            if policy not in {"case_finish", "combined"}:
                finished = True
                break
            states[current]["closed"] = True
        else:
            result = {"exit_code": 1, "stdout": "", "stderr": "Only read_file and finish are supported in this replay."}
            for state in states:
                for index in range(1, state["required_reads"] + 1):
                    if command.action != "read_file" or command.path != f"work/{state['id']}/step{index}.json":
                        continue
                    result["stderr"] = "Fixture file not found."
                    if state["solvable"]:
                        state["reads"].add(index)
                        state["complete"] = len(state["reads"]) == state["required_reads"]
                        body = ({"next_file": f"work/{state['id']}/step{index + 1}.json"}
                                if index < state["required_reads"] else {"fact": f"fixture-{state['id']}-verified"})
                        result = {"exit_code": 0, "stdout": json.dumps(body), "stderr": ""}
            history.append({"action": command.action, "path": command.path, "result": result})
            idle = 0 if states[current]["complete"] else idle + 1
            if policy in {"stall3", "combined"} and idle >= 3:
                states[current]["closed"] = True
        if states[current]["closed"]:
            pending = [i for i, state in enumerate(states) if not state["closed"]]
            if not pending:
                finished = command.action == "finish"
                break
            current, idle = pending[0], 0
    completed = sum(s["complete"] for s in states)
    return {"policy": policy, "completed_tasks": completed, "expected_tasks": sum(s["solvable"] for s in states),
            "abandoned_tasks": sum(s["closed"] and not s["complete"] for s in states),
            "missed_solvable_tasks": sum(s["solvable"] and not s["complete"] for s in states),
            "success": not failure and finished and all(s["complete"] for s in states if s["solvable"]),
            "decision_calls": len(trace), "elapsed_seconds": round(time.monotonic() - started, 3),
            "model_failure": failure, "trace": trace}


def main():
    from model_settings import default_model_settings
    defaults = default_model_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cases", default="two_cases")
    parser.add_argument("--policies", default="original")
    parser.add_argument("--max-calls", type=int, default=8)
    parser.add_argument("--num-ctx", type=int, default=defaults["options"]["num_ctx"])
    parser.add_argument("--num-predict", type=int, default=defaults["options"]["num_predict"])
    parser.add_argument("--seconds-per-run", type=int, default=240)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases, policies = args.cases.split(","), args.policies.split(",")
    if (args.model.startswith("openrouter:") or any(c not in CASES for c in cases)
            or any(p not in POLICIES for p in policies) or not 1 <= args.max_calls <= 12
            or not 60 <= args.seconds_per_run <= 600):
        parser.error("Use a local model, known cases/policies, 1–12 calls and a 60–600 second run limit")
    from harness import Harness, SYSTEM_PROMPT
    from session_log import SessionLog

    common = ("\nThis is a controlled OFFLINE replay. There is no browser, shell or real site. "
              "Use read_file to follow fixture references and finish to report. Never run code. "
              "A successful read is intermediate work; a case is complete only after its final fact. "
              "Inspect all listed cases. Missing fixture files may be reported as blocked.\n")
    settings = {**defaults, "keep_alive": "1m", "options": {
        **defaults["options"], "seed": args.seed, "num_ctx": args.num_ctx, "num_predict": args.num_predict}}
    report = {"scope": "offline model decision replay; no website/code execution or vulnerability accuracy measurement",
              "model": args.model, "settings": settings, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        for policy in policies:
            extra = ("finish ends ONLY the current case and the controller then selects the next. Focus on the current case."
                     if policy in {"case_finish", "combined"} else
                     "finish ends the ENTIRE run. Account for all listed cases before finishing.")
            if policy in {"stall3", "combined"}:
                extra += " The controller skips a case after three executions without reaching its completion phase."
            print(f"START {case} / {policy}", flush=True)
            with tempfile.TemporaryDirectory(prefix="harness-policy-") as directory:
                log = SessionLog(Path(directory), uuid.uuid4().hex, {})
                agent = Harness(log, max_calls=args.max_calls)
                deadline = time.monotonic() + args.seconds_per_run
                def decide(context):
                    return agent.decide(log.run_id, context, model=args.model, settings=settings, deadline=deadline)
                try:
                    with patch("harness.SYSTEM_PROMPT", SYSTEM_PROMPT + common + extra):
                        row = evaluate(CASES[case], policy, decide, max_calls=args.max_calls)
                    row.update(case=case, provider_calls=agent.calls,
                               system_prompt_sha256=hashlib.sha256((SYSTEM_PROMPT + common + extra).encode()).hexdigest(),
                               eligible_for_policy_comparison=not bool(row["model_failure"]),
                               **profile_events(log))
                    report["results"].append(row)
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(json.dumps({k: v for k, v in row.items() if k not in {"trace", "raw_outputs"}}), flush=True)
                finally:
                    log.close()


if __name__ == "__main__":
    main()
