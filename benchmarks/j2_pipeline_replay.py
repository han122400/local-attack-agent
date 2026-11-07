"""Deterministic fixture comparison. No model, browser, generated code or HTTP execution.

The same deliberately simple fixture selector receives reference-only or progress context.
This tests metadata utility, NOT real model behavior or vulnerability discovery accuracy.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from command_runtime import ExecutionResult
from j2_policy import J2Policy
from web_discovery import analyze_response


ROOT = "https://fixture.invalid/"
FILES = {ROOT + "alpha.87ab.js": 'const title = "fixture-alpha";',
         ROOT + "beta.6def.js": 'const title = "fixture-beta";'}


def fixture_decision(context):
    """A fixed reference follower, not an LLM or the production planner."""
    if "pending_resources" in context:
        remaining = context["pending_resources"]
    else:
        remaining = context["discovery"]["assets"]
    return {"action": "analyze", "url": remaining[0]["url"]} if remaining else {"action": "finish", "claims_complete": True}


def evaluate(decide, *, progress=True, max_calls=6):
    if type(max_calls) is not int or not 1 <= max_calls <= 100:
        raise ValueError("Use 1–100 fixture decisions")
    policy = J2Policy()
    html = "".join(f'<script src="{url}"></script>' for url in FILES)
    summary = {"source_url": ROOT, **analyze_response(html, ROOT, content_type="text/html")}
    policy.record(ExecutionResult("fixture-root", "run_python", exit_code=0, stdout="fixture root analyzed",
        discovery_summaries=[summary]), 1)
    analyzed, trace = set(), []
    redundant = invalid = unsupported = 0
    finished = False
    for index in range(max_calls):
        context = policy.context()
        if not progress:
            for key in ("pending_resources", "omitted_pending", "finish_review", "recovery", "omitted_recovery", "progress_limits"):
                context.pop(key, None)
        decision = decide(context)
        trace.append(decision)
        if decision.get("action") == "finish":
            finished = True
            unsupported += bool(decision.get("claims_complete") and len(analyzed) < len(FILES))
            break
        url = decision.get("url")
        if decision.get("action") != "analyze" or not isinstance(url, str) or url not in FILES:
            invalid += 1
            continue
        redundant += url in analyzed
        analyzed.add(url)
        summary = {"source_url": url, **analyze_response(FILES[url], url, content_type="text/javascript")}
        policy.record(ExecutionResult(f"fixture-{index}", "run_python", exit_code=0,
            stdout=FILES[url], discovery_summaries=[summary]), index + 2)
    return {"mode": "progress" if progress else "references_only_ablation",
        "expected_resources": len(FILES), "analyzed_expected_resources": len(analyzed),
        "missed_expected_resources": len(FILES) - len(analyzed), "redundant_analyses": redundant,
        "unsupported_completion_claims": unsupported, "invalid_actions": invalid,
        "finished": finished, "decision_calls": len(trace), "external_requests": 0, "trace": trace,
        "finish_review": policy.context()["finish_review"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"scope": "deterministic metadata ablation with the same fixture selector; not an old-commit benchmark, model comparison, security validation or measured discovery gain",
              "results": [evaluate(fixture_decision, progress=False), evaluate(fixture_decision, progress=True)]}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
