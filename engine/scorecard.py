"""Read-only quality scorecard for exploration runs.

Scores v4 runs from their replayed events.jsonl and older runs from session.json findings.
It never touches the lab, browser, Ollama, or the controller lease, so it is
safe to run while an experiment is in progress — an in-flight run with a partial/locked session.json
is simply skipped.

The composite score is a convenience for A/B comparison; the raw columns are the real signal. Weights
live in SCORE_WEIGHTS and are meant to be tuned.

Usage (from the project folder):
    python -m tools.scorecard                     # table of every finished run, newest last
    python -m tools.scorecard <run_id|prefix>     # per-step detail for one run
    python -m tools.scorecard --group model       # aggregate mean/stdev grouped by a field
    python -m tools.scorecard --group num_ctx     # ... or by context size, goal, scenario_source, model
    python -m tools.scorecard --json              # machine-readable per-run metrics
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from engine.paths import RUNS_ROOT


# Composite-score weights (sum to 1.0). Tune freely; the raw metrics below do not depend on them.
SCORE_WEIGHTS = {"evidence_rate": 0.5, "non_waste_rate": 0.3, "clean_finish": 0.2}


def _num_ctx(session: dict) -> int | None:
    top = session.get("num_ctx")
    if isinstance(top, int):
        return top
    opts = (session.get("model_settings") or {}).get("options") or {}
    value = opts.get("num_ctx")
    return value if isinstance(value, int) else None


def _count_compactions(run_dir: Path) -> int | None:
    """Count context_compacted events; None if the log is unreadable (e.g. an in-flight run)."""
    events = run_dir / "events.jsonl"
    if not events.exists():
        return None
    try:
        count = 0
        with events.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("kind") == "context_compacted":
                        count += 1
                except json.JSONDecodeError:
                    continue
        return count
    except OSError:
        return None


def _v4_metrics(session: dict, run_dir: Path) -> dict:
    """Measure preserved observations, not model-authored vulnerability verdicts."""
    from engine.tree import replay_tree
    from engine.usage import summarize_usage
    from engine.risk_rubric import fingerprint
    from engine.session_log import LogFailure

    unavailable = {key: None for key in ("steps", "evidence", "inconclusive", "failed", "repeats",
                                         "unique_obs", "compactions", "score")}
    saved_usage = session.get("v4_usage") or {}
    unavailable.update(instrumented=False, metrics_version="v4-process-v1", metrics_error="unreadable_events",
                       cost_usd=saved_usage.get("cost_usd"), known_cost_usd=saved_usage.get("known_cost_usd"),
                       unknown_cost_requests=saved_usage.get("unknown_cost_requests"))
    try:
        path = run_dir / "events.jsonl"
        if path.stat().st_size > 64 * 1024 * 1024:
            return unavailable
        with path.open(encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
        state = replay_tree(events, session.get("run_id", run_dir.name))
        if state["root_id"] is None:
            return unavailable
        costs = summarize_usage(events, state)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, LogFailure):
        return unavailable

    execution_calls = {item["call_id"] for item in state["executions"].values()}
    attempts = [item for item in state["attempts"].values() if item["call_id"] in execution_calls]
    steps, evidence, inconclusive, repeats = len(attempts), 0, 0, 0
    seen = set()
    for attempt in attempts:
        if attempt["status"] != "imported" or attempt["execution_status"] != "succeeded":
            continue
        refs = [ref for ref in attempt["evidence_refs"] if ref.get("source") == "model_file" and ref["bytes"] > 0]
        if not refs:
            inconclusive += 1
            continue
        evidence += 1
        identity = (attempt["node_id"], fingerprint(attempt["result"].get("observations", {})),
                    tuple(sorted({ref["sha256"] for ref in refs})))
        repeats += identity in seen
        seen.add(identity)
    failed_executions = {key for key, item in state["executions"].items()
                         if "finish" in item and (item["finish"]["exit_code"] != 0
                         or item["finish"]["timed_out"] or item["finish"]["cancelled"])}
    failed = len(failed_executions) + sum(item["status"] == "failed" and item.get("exec_id") not in failed_executions
                                         for item in attempts)
    score = None
    if steps:
        score = round(SCORE_WEIGHTS["evidence_rate"] * evidence / steps
            + SCORE_WEIGHTS["non_waste_rate"] * max(0, 1 - (repeats + inconclusive + failed) / steps)
            + SCORE_WEIGHTS["clean_finish"] * (session.get("run_result") == "completed"), 3)
    return {"metrics_version": "v4-process-v1", "metrics_error": None, "instrumented": True,
            "steps": steps, "evidence": evidence, "inconclusive": inconclusive, "failed": failed,
            "repeats": repeats, "unique_obs": len(seen), "score": score,
            "compactions": sum(event["kind"] == "context_compacted" for event in events),
            "cost_usd": costs["cost_usd"], "known_cost_usd": costs["known_cost_usd"],
            "unknown_cost_requests": costs["unknown_cost_requests"]}


def score_run(session: dict, run_dir: Path) -> dict:
    """Metrics for one run from its session dict. Pure; tolerant of missing/older fields."""
    findings = session.get("findings") or []
    steps = len(findings)
    # Runs from before the evidence flag landed have no "evidence" key at all; their evidence/
    # inconclusive split is not trustworthy, so mark them and keep them out of quality headlines.
    instrumented = any("evidence" in f for f in findings)
    evidence = sum(1 for f in findings if f.get("evidence"))
    inconclusive = sum(1 for f in findings if f.get("status") == "ok" and not f.get("evidence"))
    failed = sum(1 for f in findings if f.get("status") in ("error", "timeout"))
    repeats = sum(1 for f in findings if f.get("repeat_of"))
    # Coverage proxy: distinct evidence-bearing observations (dwelling on one page does not add here).
    unique_obs = len({f.get("observed") for f in findings if f.get("evidence")})
    result = session.get("run_result")
    clean_finish = result == "completed"

    evidence_rate = evidence / steps if steps else 0.0
    non_waste_rate = 1.0 - ((repeats + inconclusive + failed) / steps) if steps else 0.0
    score = (SCORE_WEIGHTS["evidence_rate"] * evidence_rate
             + SCORE_WEIGHTS["non_waste_rate"] * max(0.0, non_waste_rate)
             + SCORE_WEIGHTS["clean_finish"] * (1.0 if clean_finish else 0.0))

    started, finished = session.get("started_at"), session.get("finished_at")
    duration = round(finished - started) if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None

    metrics = {
        "run_id": session.get("run_id", run_dir.name),
        "model": session.get("model"),
        "num_ctx": _num_ctx(session),
        "result": result,
        "goal": (session.get("goal") or "")[:60],
        "scenario_source": session.get("scenario_source"),
        "instrumented": instrumented,
        "steps": steps,
        "evidence": evidence,
        "inconclusive": inconclusive,
        "failed": failed,
        "repeats": repeats,
        "unique_obs": unique_obs,
        "compactions": _count_compactions(run_dir),
        "clean_finish": clean_finish,
        "duration_s": duration,
        "cost_usd": session.get("cost_usd"),
        "score": round(score, 3),
    }
    if session.get("experiment_mode") == "v4":
        metrics.update(_v4_metrics(session, run_dir))
    return metrics


def load_runs(runs_root: Path = RUNS_ROOT) -> list[dict]:
    """Every readable, finished run, oldest first by start time. In-flight/unfinished runs are skipped."""
    rows = []
    for run_dir in sorted(p.parent for p in runs_root.glob("*/session.json")):
        try:
            session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # partial write / locked while running
        if not session.get("findings") and session.get("run_result") is None:
            continue  # nothing scored yet (likely in flight)
        rows.append(score_run(session, run_dir))
    rows.sort(key=lambda r: (r["duration_s"] is None, r.get("run_id")))
    return rows


_COLS = [("run_id", 12), ("model", 12), ("num_ctx", 7), ("result", 10), ("instrumented", 5),
         ("steps", 5), ("evidence", 4), ("inconclusive", 5), ("failed", 6), ("repeats", 7),
         ("unique_obs", 4), ("compactions", 6), ("duration_s", 8), ("cost_usd", 9), ("score", 6)]


def _fmt(value: object, width: int) -> str:
    if value is None:
        value = "-"
    elif value is True:
        value = "Y"
    elif value is False:
        value = "N"
    text = str(value)
    if isinstance(value, str) and len(text) > width:
        text = text[:width]
    return text.ljust(width)


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No finished runs found under", RUNS_ROOT)
        return
    header = "  ".join(name[:width].ljust(width) for name, width in _COLS)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for name, width in _COLS:
            value = row["run_id"][:12] if name == "run_id" else row.get(name)
            cells.append(_fmt(value, width))
        print("  ".join(cells))
    scores = [r["score"] for r in rows if r["score"] is not None]
    print("-" * len(header))
    evless = sum(1 for r in rows if r["instrumented"] and r["clean_finish"] and r["evidence"] == 0)
    legacy = sum(1 for r in rows if not r["instrumented"])
    mean = f"{statistics.mean(scores):.3f}" if scores else "-"
    print(f"runs={len(rows)}  score mean={mean}"
          + (f"  stdev={statistics.pstdev(scores):.3f}" if len(scores) > 1 else "")
          + f"  evidence-less completed={evless} (of instrumented)"
          + (f"  ·  {legacy} legacy run(s) pre-evidence-flag" if legacy else ""))


def print_detail(rows: list[dict], query: str) -> None:
    matches = [r for r in rows if r["run_id"].startswith(query)]
    if not matches:
        print(f"No finished run matches '{query}'.")
        return
    row = matches[0]
    run_dir = RUNS_ROOT / row["run_id"]
    session = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
    print(f"run {row['run_id']}  model={row['model']}  num_ctx={row['num_ctx']}  result={row['result']}")
    print(f"goal: {session.get('goal')}")
    print(f"score={row['score']}  steps={row['steps']}  evidence={row['evidence']}  "
          f"inconclusive={row['inconclusive']}  failed={row['failed']}  repeats={row['repeats']}  "
          f"compactions={row['compactions']}  duration={row['duration_s']}s")
    print("-" * 80)
    for f in session.get("findings") or []:
        flag = "EV" if f.get("evidence") else ("!!" if f.get("status") in ("error", "timeout") else "..")
        rep = f"  <repeat of step {f['repeat_of']}>" if f.get("repeat_of") else ""
        print(f"  {f.get('step'):>2} [{flag}] {f.get('action'):<11} {str(f.get('observed'))[:70]}{rep}")
    reason = session.get("finish_reason") or session.get("report")
    if reason:
        print("-" * 80)
        print("finish/report:", str(reason)[:600])


def print_group(rows: list[dict], field: str) -> None:
    buckets: dict[object, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row.get(field), []).append(row)
    print(f"group by {field}")
    print(f"{'value':<28} {'n':>3} {'score mean':>11} {'stdev':>7} {'ev mean':>8} {'steps':>6} {'ev-less done':>12}")
    print("-" * 80)
    for value, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        scores = [r["score"] for r in group if r["score"] is not None]
        evid = [r["evidence"] for r in group if r["evidence"] is not None]
        steps = [r["steps"] for r in group if r["steps"] is not None]
        evless = sum(1 for r in group if r["instrumented"] and r["clean_finish"] and r["evidence"] == 0)
        mean = f"{statistics.mean(scores):.3f}" if scores else "-"
        ev_mean = f"{statistics.mean(evid):.1f}" if evid else "-"
        step_mean = f"{statistics.mean(steps):.1f}" if steps else "-"
        print(f"{str(value)[:28]:<28} {len(group):>3} {mean:>11} "
              f"{(statistics.pstdev(scores) if len(scores) > 1 else 0):>7.3f} "
              f"{ev_mean:>8} {step_mean:>6} {evless:>12}")


def main(argv: list[str]) -> int:
    rows = load_runs()
    if "--json" in argv:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if "--group" in argv:
        idx = argv.index("--group")
        field = argv[idx + 1] if idx + 1 < len(argv) else "model"
        print_group(rows, field)
        return 0
    positional = [a for a in argv if not a.startswith("--")]
    if positional:
        print_detail(rows, positional[0])
        return 0
    print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
