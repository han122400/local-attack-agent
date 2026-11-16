"""Scripted decision-contract comparison; no model, browser, generated code or HTTP runs."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import Harness
from command_runtime import ExecutionResult
from j25_runner import J25Runner
from planning import load_prompt
from session_log import SessionLog


def evaluate():
    results = []
    for version in ("J2", "J2.5"):
        with tempfile.TemporaryDirectory() as temp:
            run_id = "6" * 32
            metadata = {"goal": "Compare a known search input", "target": "https://fixture.invalid/", "model": "fixture",
                        "model_settings": {}, "budgets": {"seconds": 300, "model_calls": 3,
                                                              "tool_calls": 4, "command_seconds": 30}}
            log = SessionLog(Path(temp), run_id, metadata)
            try:
                prompt, digest = load_prompt(version)
                if version == "J2":
                    decisions = [
                        {"action": "run_python", "plan": "Compare a search input", "reason": "Collect controls",
                         "path": "", "content": "", "command": "", "code":
                         "a = await http_get(target_origin + '/search?q=normal')\n"
                         "b = await http_get(target_origin + '/search?q=test')\n"
                         "c = await http_get(target_origin + '/search?q=normal')\nprint(a['status'], b['status'], c['status'])"},
                        {"action": "finish", "plan": "Report", "reason": "Fixture comparison recorded",
                         "path": "", "content": "", "command": "", "code": ""}]
                else:
                    decisions = [{"action": "compare", "ref": "I1", "reason": "Collect controls",
                                  "hypothesis": "Search input changes response", "normal": "normal", "test": "test"},
                                 {"action": "finish", "reason": "Fixture comparison recorded"}]
                sent = []
                comparison_status = None
                def transport(payload):
                    decision = decisions[len(sent)]
                    sent.append(payload)
                    yield {"message": {"content": json.dumps(decision)}, "done": True}
                if version == "J2":
                    harness = Harness(log, transport=transport, max_calls=3, report_reserve=1,
                                      system_prompt=prompt, system_prompt_version=version, system_prompt_sha256=digest)
                    context = {"goal": metadata["goal"], "target": metadata["target"], "remaining": {"finish_soon": False}}
                    action = harness.decide(run_id, context, model="fixture", settings={})
                    context["remaining"]["finish_soon"] = True
                    finish = harness.decide(run_id, context, model="fixture", settings={})
                    steps, model_calls, generated = [action.action], harness.calls, int(action.action == "run_python")
                    finish_action = finish.action
                else:
                    class FixtureRuntime:
                        def __init__(self):
                            self.actions = []
                        def run(self, run_id, command, *, call_id, timeout):
                            action = command["decision"]["action"]
                            self.actions.append(action)
                            refs = {"pages": [{"id": "P1", "url": metadata["target"]}], "assets": [],
                                    "inputs": [{"id": "I1", "url": metadata["target"] + "search", "field": "q"}]}
                            result = {"action": action, "ref": "P1" if action == "start" else "I1",
                                      "status": "ok", "references": refs, "omitted": {}, "request_count": 0}
                            if action == "compare":
                                result.update(request_count=3, comparison={"samples": [
                                    {"stage": "normal", "status": 200}, {"stage": "test", "status": 200},
                                    {"stage": "recheck", "status": 200}], "baseline_stable": True})
                            return ExecutionResult(f"EXEC-{len(self.actions)}", "j25", exit_code=0, j25_result=result)
                    runtime = FixtureRuntime()
                    runner = J25Runner(run_id, log, runtime, transport=transport, stop_requested=lambda: False)
                    runner.run()
                    comparison_status = next((item["status"] for item in runner.state["steps"]
                                              if item["action"] == "compare"), None)
                    steps, model_calls, generated, finish_action = runtime.actions, runner.harness.calls, 0, "finish"
                comparison_step = next(index for index, item in enumerate(steps, 1)
                                       if item in {"run_python", "compare"})
                results.append({"version": version, "valid_decisions": len(sent), "model_calls": model_calls,
                    "first_comparison_step": comparison_step, "generated_code_commands": generated,
                    "execution_failures": 0, "model_failures": 0, "finish": finish_action,
                    "comparison_status": comparison_status,
                    "comparison": "simulated fixture only; no generated code executed"})
            finally:
                log.close()
    return {"scope": "scripted decision replay; not a live model or vulnerability performance comparison",
            "external_requests": 0, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(evaluate(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
